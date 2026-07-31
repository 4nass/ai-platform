"""Tests for providers.claude_code.adapter."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from providers.base import AgentTask
from providers.claude_code import adapter


def _task(
    agent: str = "backend",
    context_paths: list[str] | None = None,
    context_render: str = "",
    repo_root: Path = Path("."),
    engine_root: Path | None = None,
) -> AgentTask:
    return AgentTask(
        agent=agent,
        description="do x",
        repo_root=repo_root,
        engine_root=engine_root,
        context_paths=context_paths or [],
        context_render=context_render,
    )


def _completed(cmd, returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(cmd, returncode=returncode, stdout=stdout, stderr=stderr)


def _mock_run_sequence(*results):
    calls = iter(results)

    def fake_run(cmd, **kwargs):
        result = next(calls)
        if isinstance(result, Exception):
            raise result
        return result

    return fake_run


# --- pure helpers ---


def test_allowed_tools_default_role() -> None:
    assert adapter._allowed_tools("backend") == adapter.DEFAULT_ALLOWED_TOOLS


def test_allowed_tools_reviewer_is_read_only() -> None:
    assert adapter._allowed_tools("reviewer") == "Read,Grep,Glob"


def test_allowed_tools_security_is_read_only() -> None:
    assert adapter._allowed_tools("security") == "Read,Grep,Glob"


def test_allowed_tools_architect_can_write_but_not_run_commands() -> None:
    tools = adapter._allowed_tools("architect")
    assert "Write" in tools
    assert "Bash" not in tools


def test_build_prompt_without_context_is_just_the_description() -> None:
    assert adapter._build_prompt(_task()) == "do x"


def test_build_prompt_injects_the_rendered_context() -> None:
    prompt = adapter._build_prompt(_task(context_render="## Selected context\n  1. a.py — semantic match"))

    assert "do x" in prompt
    assert "## Selected context" in prompt
    assert "1. a.py — semantic match" in prompt


def test_build_prompt_sends_the_context_it_was_given_not_a_path_listing() -> None:
    """The regression this whole step exists for: the adapter used to rebuild
    its own flat listing from context_paths and drop context_render entirely,
    so everything the context layer computed never reached the model."""
    prompt = adapter._build_prompt(
        _task(context_paths=["a.py", "b.py"], context_render="RENDERED CONTEXT")
    )

    assert "RENDERED CONTEXT" in prompt
    assert "- b.py" not in prompt


def test_build_prompt_ignores_paths_when_there_is_nothing_rendered() -> None:
    """context_paths is the machine-readable list for telemetry, not prompt
    material — an empty rendering means nothing to inject."""
    assert adapter._build_prompt(_task(context_paths=["a.py"])) == "do x"


# --- _check_auth ---


def test_check_auth_logged_in_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter.subprocess, "run", lambda cmd, **kw: _completed(cmd, 0, '{"loggedIn": true}'))

    assert adapter._check_auth() is None


def test_check_auth_logged_out_returns_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter.subprocess, "run", lambda cmd, **kw: _completed(cmd, 1, '{"loggedIn": false}'))

    error = adapter._check_auth()

    assert error is not None
    assert "not logged in" in error.lower()


def test_check_auth_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_missing(cmd, **kw):
        raise FileNotFoundError()

    monkeypatch.setattr(adapter.subprocess, "run", raise_missing)

    error = adapter._check_auth()

    assert error is not None
    assert "not found in PATH" in error


def test_check_auth_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=30)

    monkeypatch.setattr(adapter.subprocess, "run", raise_timeout)

    error = adapter._check_auth()

    assert error is not None
    assert "timed out" in error


def test_check_auth_non_json_output_treated_as_not_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter.subprocess, "run", lambda cmd, **kw: _completed(cmd, 0, "not json"))

    assert adapter._check_auth() is not None


# --- run() ---


def test_run_short_circuits_when_not_authenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter.subprocess, "run", lambda cmd, **kw: _completed(cmd, 1, '{"loggedIn": false}'))

    result = adapter.run(_task())

    assert result.success is False
    assert "not logged in" in result.summary.lower()


def test_run_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        _mock_run_sequence(
            _completed(["claude", "auth"], 0, '{"loggedIn": true}'),
            _completed(["claude", "-p"], 0, '{"result": "done", "is_error": false}'),
        ),
    )

    result = adapter.run(_task())

    assert result.success is True
    assert result.summary == "done"


def test_run_loads_the_system_prompt_from_engine_root_not_repo_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A --repo target has no prompts/ directory of its own -- the role
    prompt always comes from the engine install, never from wherever the
    task happens to write files."""
    engine_root = tmp_path / "engine"
    target_root = tmp_path / "target"
    (engine_root / "prompts").mkdir(parents=True)
    (engine_root / "prompts" / "backend.md").write_text("You are the Backend Agent.", encoding="utf-8")
    target_root.mkdir()

    captured_cmd = {}

    def fake_run(cmd, **kw):
        captured_cmd["cmd"] = cmd
        if cmd[0:2] == ["claude", "auth"]:
            return _completed(cmd, 0, '{"loggedIn": true}')
        return _completed(cmd, 0, '{"result": "done", "is_error": false}')

    monkeypatch.setattr(adapter.subprocess, "run", fake_run)

    result = adapter.run(_task(repo_root=target_root, engine_root=engine_root))

    assert result.success is True
    cmd = captured_cmd["cmd"]
    assert "--append-system-prompt" in cmd
    assert cmd[cmd.index("--append-system-prompt") + 1] == "You are the Backend Agent."


def test_run_is_error_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        _mock_run_sequence(
            _completed(["claude", "auth"], 0, '{"loggedIn": true}'),
            _completed(["claude", "-p"], 0, '{"result": "refused", "is_error": true}'),
        ),
    )

    result = adapter.run(_task())

    assert result.success is False
    assert result.summary == "refused"


def test_run_non_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        _mock_run_sequence(
            _completed(["claude", "auth"], 0, '{"loggedIn": true}'),
            _completed(["claude", "-p"], 0, "not json at all"),
        ),
    )

    result = adapter.run(_task())

    assert result.success is False
    assert "Non-JSON" in result.summary


def test_run_non_zero_exit_with_json_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        _mock_run_sequence(
            _completed(["claude", "auth"], 0, '{"loggedIn": true}'),
            _completed(["claude", "-p"], 1, '{"result": "boom"}'),
        ),
    )

    result = adapter.run(_task())

    assert result.success is False
    assert "boom" in result.summary


def test_run_non_zero_exit_without_json_falls_back_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        _mock_run_sequence(
            _completed(["claude", "auth"], 0, '{"loggedIn": true}'),
            _completed(["claude", "-p"], 1, "", "segfault"),
        ),
    )

    result = adapter.run(_task())

    assert result.success is False
    assert "segfault" in result.summary


def test_run_missing_binary_on_main_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        _mock_run_sequence(
            _completed(["claude", "auth"], 0, '{"loggedIn": true}'),
            FileNotFoundError(),
        ),
    )

    result = adapter.run(_task())

    assert result.success is False
    assert "not found in PATH" in result.summary


# --- usage parsing ---

# Trimmed from a real `claude -p --output-format json` response captured
# against the authenticated CLI, so the field names are the CLI's, not ours.
REAL_PAYLOAD = """{
  "is_error": false,
  "result": "done",
  "duration_ms": 3444,
  "duration_api_ms": 2290,
  "total_cost_usd": 0.0614727,
  "usage": {
    "input_tokens": 2,
    "output_tokens": 4,
    "cache_creation_input_tokens": 9255,
    "cache_read_input_tokens": 19589
  },
  "modelUsage": {"claude-sonnet-5": {"costUSD": 0.0614727}}
}"""


def test_parse_usage_from_a_real_payload() -> None:
    usage = adapter._parse_usage(json.loads(REAL_PAYLOAD))

    assert usage is not None
    assert usage.model == "claude-sonnet-5"
    assert usage.input_tokens == 2
    assert usage.output_tokens == 4
    assert usage.cache_creation_tokens == 9255
    assert usage.cache_read_tokens == 19589
    assert usage.cost_usd == pytest.approx(0.0614727)
    assert usage.provider_duration_ms == 3444


def test_run_attaches_usage_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        _mock_run_sequence(
            _completed(["claude", "auth"], 0, '{"loggedIn": true}'),
            _completed(["claude", "-p"], 0, REAL_PAYLOAD),
        ),
    )

    result = adapter.run(_task())

    assert result.usage is not None
    assert result.usage.cost_usd == pytest.approx(0.0614727)


def test_run_attaches_usage_on_failure_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed call still burned tokens — dropping its usage would understate
    what the run actually cost."""
    payload = '{"is_error": true, "result": "refused", "usage": {"input_tokens": 40}, "total_cost_usd": 0.01}'
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        _mock_run_sequence(
            _completed(["claude", "auth"], 0, '{"loggedIn": true}'),
            _completed(["claude", "-p"], 0, payload),
        ),
    )

    result = adapter.run(_task())

    assert result.success is False
    assert result.usage is not None
    assert result.usage.input_tokens == 40
    assert result.usage.cost_usd == pytest.approx(0.01)


def test_parse_usage_degrades_to_zeros_on_unexpected_shape() -> None:
    """The CLI's output shape is not a contract we control — a change in it
    must not be the thing that breaks a run."""
    usage = adapter._parse_usage({"usage": "not a dict", "total_cost_usd": "free", "modelUsage": []})

    assert usage is not None
    assert usage.input_tokens == 0
    assert usage.cost_usd is None
    assert usage.model == ""


def test_parse_usage_returns_none_for_non_dict() -> None:
    assert adapter._parse_usage(None) is None


def test_parse_usage_records_every_model_when_a_call_spans_several() -> None:
    usage = adapter._parse_usage({"modelUsage": {"claude-opus-5": {}, "claude-sonnet-5": {}}})

    assert usage is not None
    assert usage.model == "claude-opus-5, claude-sonnet-5"
