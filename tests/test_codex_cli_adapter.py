"""Tests for providers.codex_cli.adapter.

The event fixtures below are the real bytes emitted by codex-cli 0.146.0 for a
trivial prompt, captured from a live call — not a hand-written approximation.
That matters most for the usage figures: the whole point of the normalization
is that codex's shape differs from Anthropic's in a way no reasonable guess
would produce.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from providers.base import AgentTask
from providers.codex_cli import adapter

# Captured verbatim from `codex exec --json --sandbox read-only`.
REAL_EVENTS = (
    '{"type":"thread.started","thread_id":"019fb916-55d3-7cd3-b372-3cd6bb73f081"}\n'
    '{"type":"turn.started"}\n'
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"PONG"}}\n'
    '{"type":"turn.completed","usage":{"input_tokens":13994,"cached_input_tokens":11008,'
    '"cache_write_input_tokens":0,"output_tokens":6,"reasoning_output_tokens":0}}\n'
)


def _task(agent: str = "backend", context_render: str = "") -> AgentTask:
    return AgentTask(
        agent=agent, description="do x", repo_root=Path("."), context_render=context_render
    )


def _completed(cmd, returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(cmd, returncode=returncode, stdout=stdout, stderr=stderr)


def _mock_run_sequence(*results):
    """Replays results in order. `run()` always calls the auth preflight first,
    so a full invocation needs two entries."""
    calls = iter(results)
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        result = next(calls)
        if isinstance(result, Exception):
            raise result
        return result(cmd) if callable(result) else result

    fake_run.captured = captured
    return fake_run


def _authed():
    return _completed(["codex", "login", "status"], stdout="Logged in using ChatGPT")


# --- the token convention: the reason this adapter exists in this shape ---


def test_cached_tokens_are_subtracted_because_codex_counts_them_inside_input() -> None:
    """Codex follows the OpenAI convention, where `cached_input_tokens` is a
    subset of `input_tokens`. The platform wants them disjoint. Passing the
    numbers through unchanged would report 25,002 tokens for a 13,994-token
    prompt — the step-1 "28 in" bug in reverse."""
    usage = adapter._parse_usage(
        {
            "input_tokens": 13994,
            "cached_input_tokens": 11008,
            "cache_write_input_tokens": 0,
            "output_tokens": 6,
            "reasoning_output_tokens": 0,
        }
    )

    assert usage.input_tokens == 2986  # 13994 - 11008
    assert usage.cache_read_tokens == 11008
    assert usage.input_tokens + usage.cache_read_tokens + usage.cache_creation_tokens == 13994


def test_reasoning_tokens_are_not_added_to_output() -> None:
    """`reasoning_output_tokens` is a subset of `output_tokens`, like the
    cached figure is of the input one."""
    usage = adapter._parse_usage(
        {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 40, "reasoning_output_tokens": 30}
    )

    assert usage.output_tokens == 40


def test_a_cached_count_larger_than_input_cannot_go_negative() -> None:
    usage = adapter._parse_usage({"input_tokens": 10, "cached_input_tokens": 99})

    assert usage.input_tokens == 0


def test_usage_reports_no_cost_because_a_subscription_prices_nothing_per_call() -> None:
    """None is 'unknown', not 'free' — a 0.0 here would make codex look
    cheaper than claude_code rather than differently accounted."""
    usage = adapter._parse_usage({"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 5})

    assert usage.cost_usd is None


def test_garbage_usage_degrades_to_none_rather_than_raising() -> None:
    assert adapter._parse_usage(None) is None
    assert adapter._parse_usage("not a dict") is None


def test_missing_usage_fields_degrade_to_zero() -> None:
    usage = adapter._parse_usage({})

    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (0, 0, 0)


# --- role -> sandbox: tool restriction in this CLI's vocabulary ---


@pytest.mark.parametrize("agent", ["reviewer", "security", "decomposer"])
def test_report_only_roles_run_in_a_read_only_sandbox(agent: str) -> None:
    """Their prompts say they produce a report, not a modification. The
    sandbox holds them to it instead of trusting the instruction."""
    assert adapter._sandbox(agent) == "read-only"


@pytest.mark.parametrize("agent", ["backend", "frontend", "architect", "documentation", "tests"])
def test_editing_roles_get_a_writable_workspace(agent: str) -> None:
    assert adapter._sandbox(agent) == "workspace-write"


# --- prompt construction ---


def test_prompt_carries_the_role_prompt_because_there_is_no_system_prompt_flag(tmp_path: Path) -> None:
    """`codex exec` has no --append-system-prompt equivalent, so the role
    prompt rides in the message with no special authority — a real behavioural
    difference from claude_code, not just a wiring detail."""
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "backend.md").write_text("ROLE INSTRUCTIONS", encoding="utf-8")
    task = AgentTask(agent="backend", description="do x", repo_root=tmp_path)

    prompt = adapter._build_prompt(task)

    assert "ROLE INSTRUCTIONS" in prompt
    assert prompt.index("ROLE INSTRUCTIONS") < prompt.index("do x")


def test_prompt_includes_the_selected_context() -> None:
    prompt = adapter._build_prompt(_task(context_render="## Selected context\n 1. a.py"))

    assert "## Selected context" in prompt


def test_prompt_without_a_role_file_is_just_the_request(tmp_path: Path) -> None:
    task = AgentTask(agent="backend", description="do x", repo_root=tmp_path)

    assert adapter._build_prompt(task) == "do x"


# --- event parsing ---


def test_parses_the_agent_message_and_usage_from_real_events() -> None:
    message, usage = adapter._parse_events(REAL_EVENTS)

    assert message == "PONG"
    assert usage["input_tokens"] == 13994


def test_unparseable_lines_are_skipped_not_fatal() -> None:
    """During real work the stream carries item types beyond a trivial probe's;
    an unrecognized line is not a reason to discard a call that happened."""
    message, usage = adapter._parse_events("not json\n\n" + REAL_EVENTS + "also not json\n")

    assert message == "PONG"
    assert usage is not None


def test_the_last_agent_message_wins() -> None:
    events = (
        '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}\n'
        '{"type":"item.completed","item":{"type":"command_execution","command":"ls"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"final"}}\n'
    )

    message, _ = adapter._parse_events(events)

    assert message == "final"


# --- run(): command shape and failure paths ---


def test_run_invokes_codex_exec_with_the_verified_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _mock_run_sequence(_authed(), _completed(["codex"], stdout=REAL_EVENTS))
    monkeypatch.setattr(subprocess, "run", fake)

    result = adapter.run(_task(agent="reviewer"))

    assert result.success is True
    cmd = fake.captured[1]
    assert cmd[:3] == ["codex", "exec", "--json"]
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "--skip-git-repo-check" in cmd
    assert "--cd" in cmd


def test_run_pins_profile_model_and_toml_string_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _mock_run_sequence(_authed(), _completed(["codex"], stdout=REAL_EVENTS))
    monkeypatch.setattr(subprocess, "run", fake)
    task = _task()
    task.model = "gpt-5.6-sol"
    task.reasoning_effort = "xhigh"

    result = adapter.run(task)

    cmd = fake.captured[1]
    assert cmd[cmd.index("--model") + 1] == "gpt-5.6-sol"
    assert cmd[cmd.index("-c") + 1] == 'model_reasoning_effort="xhigh"'
    assert result.usage.model == "gpt-5.6-sol"


def test_run_closes_stdin_so_codex_does_not_block_waiting_on_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Left open, codex reads stdin ("Reading additional input from stdin...");
    inside a worker thread that is an invisible hang, not an error."""
    seen: list[object] = []

    def fake_run(cmd, **kwargs):
        seen.append(kwargs.get("stdin"))
        if cmd[:2] == ["codex", "login"]:
            return _authed()
        return _completed(cmd, stdout=REAL_EVENTS)

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter.run(_task())

    assert seen == [subprocess.DEVNULL, subprocess.DEVNULL]


def test_run_prefers_the_output_last_message_file_over_the_event_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exec_writing_file(cmd):
        Path(cmd[cmd.index("--output-last-message") + 1]).write_text("from file\n", encoding="utf-8")
        return _completed(cmd, stdout=REAL_EVENTS)

    monkeypatch.setattr(subprocess, "run", _mock_run_sequence(_authed(), exec_writing_file))

    assert adapter.run(_task()).summary == "from file"


def test_run_falls_back_to_the_event_stream_when_the_file_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess, "run", _mock_run_sequence(_authed(), _completed(["codex"], stdout=REAL_EVENTS))
    )

    assert adapter.run(_task()).summary == "PONG"


def test_run_refuses_early_when_not_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _mock_run_sequence(_completed(["codex", "login", "status"], returncode=1))
    monkeypatch.setattr(subprocess, "run", fake)

    result = adapter.run(_task())

    assert result.success is False
    assert "codex login" in result.summary
    assert len(fake.captured) == 1  # never reached the exec call


def test_run_reports_a_missing_binary_actionably(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _mock_run_sequence(FileNotFoundError()))

    result = adapter.run(_task())

    assert result.success is False
    assert "not found in PATH" in result.summary


def test_run_reports_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        _mock_run_sequence(_authed(), subprocess.TimeoutExpired(cmd="codex", timeout=900)),
    )

    result = adapter.run(_task())

    assert result.success is False
    assert "timed out" in result.summary


def test_a_failed_call_still_reports_the_quota_it_consumed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tokens were spent before the failure; recording the call as free would
    understate pressure on the subscription."""
    monkeypatch.setattr(
        subprocess,
        "run",
        _mock_run_sequence(_authed(), _completed(["codex"], returncode=1, stdout=REAL_EVENTS, stderr="boom")),
    )

    result = adapter.run(_task())

    assert result.success is False
    assert result.usage is not None
    assert result.usage.cache_read_tokens == 11008


def test_run_fails_when_codex_produced_neither_a_message_nor_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", _mock_run_sequence(_authed(), _completed(["codex"], stdout="")))

    result = adapter.run(_task())

    assert result.success is False


def test_adapter_declares_that_it_reads_files_itself() -> None:
    assert adapter.READS_FILES is True
