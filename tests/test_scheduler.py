"""Tests for core.orchestrator.scheduler."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.context import selection
from core.context.manager import FULL, POINTERS, SelectedContext
from core.context.selection import Decision
from core.errors import ConfigError
from core.orchestrator import scheduler
from core.orchestrator.planner import Task
from core.orchestrator.scheduler import StageResult
from providers.base import AgentTask, ProviderResult, TokenUsage

AGENTS_YAML = """backend:
  provider: claude_code
no_provider: {}
"""


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "agents.yaml").write_text(AGENTS_YAML, encoding="utf-8")
    return tmp_path


def test_resolve_provider_known_agent(repo_root: Path) -> None:
    assert scheduler.resolve_provider(repo_root, "backend") == "claude_code"


def test_resolve_provider_unknown_agent_raises(repo_root: Path) -> None:
    with pytest.raises(ConfigError, match="Unknown agent role"):
        scheduler.resolve_provider(repo_root, "does_not_exist")


def test_resolve_provider_missing_provider_key_raises(repo_root: Path) -> None:
    with pytest.raises(ConfigError, match="declares no providers"):
        scheduler.resolve_provider(repo_root, "no_provider")


def test_resolve_provider_unknown_provider_name_raises(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "agents.yaml").write_text(
        "backend:\n  provider: not_a_real_provider\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="Unknown provider"):
        scheduler.resolve_provider(tmp_path, "backend")


def _fake_provider(captured: dict, reads_files: bool = True):
    def fake_run(task: AgentTask) -> ProviderResult:
        captured["task"] = task
        return ProviderResult(success=True, summary=task.description)

    return type("FakeProvider", (), {"run": staticmethod(fake_run), "READS_FILES": reads_files})


def _context(injection_mode: str = POINTERS) -> SelectedContext:
    return SelectedContext(
        chunks=[
            {
                "path": "a.py",
                "kind": "function",
                "name": "foo",
                "start_line": 1,
                "end_line": 2,
                "text": "EXCERPT BODY",
            }
        ],
        related_files=["b.py"],
        injection_mode=injection_mode,
    )


def test_run_task_dispatches_to_the_resolved_provider(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    captured: dict = {}
    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", _fake_provider(captured))

    result = scheduler.run_task(repo_root, "backend", "do the thing", _context())

    assert result.success is True
    assert captured["task"].description == "do the thing"
    assert captured["task"].context_paths == ["a.py", "b.py"]


def test_run_task_propagates_the_selected_execution_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "agents.yaml").write_text(
        "backend:\n  profiles: [{provider: codex_cli, model: gpt-x, reasoning_effort: low}]\n",
        encoding="utf-8",
    )
    captured: dict = {}
    monkeypatch.setitem(scheduler.PROVIDERS, "codex_cli", _fake_provider(captured))
    recorder = _SpyRecorder()

    scheduler.run_task(tmp_path, "backend", "x", recorder=recorder)

    assert (captured["task"].model, captured["task"].reasoning_effort) == ("gpt-x", "low")
    assert recorder.calls[0]["model"] == "gpt-x"
    assert recorder.calls[0]["reasoning_effort"] == "low"




def test_run_task_propagates_complexity_profile_and_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "agents.yaml").write_text(
        """backend:
  profiles: [{provider: codex_cli, model: gpt-5.6-terra, effort: medium}]
  profiles_by_complexity:
    critical:
      - {provider: codex_cli, model: gpt-5.6-sol, effort: xhigh}
""",
        encoding="utf-8",
    )
    captured: dict = {}
    monkeypatch.setitem(scheduler.PROVIDERS, "codex_cli", _fake_provider(captured))
    recorder = _SpyRecorder()

    scheduler.run_task(
        tmp_path, "backend", "x", recorder=recorder, complexity="critical"
    )

    task = captured["task"]
    assert (task.model, task.reasoning_effort, task.complexity) == (
        "gpt-5.6-sol", "xhigh", "critical"
    )
    assert recorder.calls[0]["metadata"]["complexity"] == "critical"

def test_run_task_renders_pointers_for_a_provider_that_reads_files(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> None:
    captured: dict = {}
    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", _fake_provider(captured, reads_files=True))

    scheduler.run_task(repo_root, "backend", "do the thing", _context(POINTERS))

    assert "EXCERPT BODY" not in captured["task"].context_render
    assert "a.py" in captured["task"].context_render


def test_run_task_renders_full_content_for_a_provider_without_disk_access(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> None:
    """The provider's shape wins over the config: pointers to files it can't
    open would leave it with no context at all."""
    captured: dict = {}
    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", _fake_provider(captured, reads_files=False))

    scheduler.run_task(repo_root, "backend", "do the thing", _context(POINTERS))

    assert "EXCERPT BODY" in captured["task"].context_render


def test_run_task_without_context_sends_nothing(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    captured: dict = {}
    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", _fake_provider(captured))

    scheduler.run_task(repo_root, "backend", "review this")

    assert captured["task"].context_render == ""
    assert captured["task"].context_paths == []


class _SpyRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record_call(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_run_task_records_the_call_with_context_sizes(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    captured: dict = {}
    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", _fake_provider(captured))
    recorder = _SpyRecorder()

    scheduler.run_task(repo_root, "backend", "do the thing", _context(), recorder=recorder, stage_id="backend")

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["agent"] == "backend"
    assert call["provider"] == "claude_code"
    assert call["stage_id"] == "backend"
    assert call["context_files"] == 2
    assert call["duration_ms"] >= 0
    assert call["started_at"]


def test_run_task_records_the_size_actually_sent(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    """context_chars has to be what the provider received, not the length of
    a rendering it never saw — otherwise the A/B between injection modes
    compares two numbers that mean the same thing."""
    captured: dict = {}
    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", _fake_provider(captured))
    recorder = _SpyRecorder()

    scheduler.run_task(repo_root, "backend", "do the thing", _context(POINTERS), recorder=recorder)

    assert recorder.calls[0]["context_chars"] == len(captured["task"].context_render)


def test_run_task_records_which_rendering_the_call_received(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> None:
    """Per call, not per run: providers of different shapes in one run get
    different renderings, so the run-level config snapshot can't tell you
    what any given call was sent."""
    recorder = _SpyRecorder()
    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", _fake_provider({}, reads_files=True))
    scheduler.run_task(repo_root, "backend", "x", _context(POINTERS), recorder=recorder)

    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", _fake_provider({}, reads_files=False))
    scheduler.run_task(repo_root, "backend", "x", _context(POINTERS), recorder=recorder)

    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", _fake_provider({}, reads_files=True))
    scheduler.run_task(repo_root, "backend", "x", _context(FULL), recorder=recorder)
    scheduler.run_task(repo_root, "backend", "x", None, recorder=recorder)

    assert [c["metadata"]["injection"] for c in recorder.calls] == ["pointers", "full", "full", "none"]


def test_run_task_without_a_recorder_is_a_no_op(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    captured: dict = {}
    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", _fake_provider(captured))

    result = scheduler.run_task(repo_root, "backend", "do the thing")

    assert result.success is True


def test_run_task_records_failed_calls_too(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    """A failed provider call still cost tokens; skipping it would understate
    the run."""

    def failing_run(task: AgentTask) -> ProviderResult:
        return ProviderResult(success=False, summary="nope")

    monkeypatch.setitem(
        scheduler.PROVIDERS, "claude_code", type("FakeProvider", (), {"run": staticmethod(failing_run)})
    )
    recorder = _SpyRecorder()

    scheduler.run_task(repo_root, "backend", "x", recorder=recorder)

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["result"].success is False


def test_build_stage_description_with_no_upstream_returns_the_request() -> None:
    assert scheduler.build_stage_description("add oauth2", []) == "add oauth2"


def test_build_stage_description_ignores_non_done_stages() -> None:
    architecture = Task(id="architecture", agent="architect", depends_on=[])
    upstream = [StageResult(task=architecture, status="skipped")]

    assert scheduler.build_stage_description("add oauth2", upstream) == "add oauth2"


def test_build_stage_description_includes_summary_and_files_of_done_stages() -> None:
    architecture = Task(id="architecture", agent="architect", depends_on=[])
    upstream = [
        StageResult(
            task=architecture,
            status="done",
            result=ProviderResult(success=True, summary="Wrote the ADR."),
            files_changed=["memory/adr/ADR-001-oauth.md"],
        )
    ]

    description = scheduler.build_stage_description("add oauth2", upstream)

    assert "add oauth2" in description
    assert "architecture (architect): Wrote the ADR." in description
    assert "memory/adr/ADR-001-oauth.md" in description


def test_build_stage_description_reports_no_files_changed_for_report_only_stages() -> None:
    security = Task(id="security", agent="security", depends_on=[])
    upstream = [
        StageResult(
            task=security,
            status="done",
            result=ProviderResult(success=True, summary="No issues found."),
            files_changed=[],
        )
    ]

    description = scheduler.build_stage_description("add oauth2", upstream)

    assert "no files changed" in description


def test_run_task_records_the_decision_log(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    """context_reason is stored per call so a `calls` row answers "why these
    files?" on its own, without a join back to the run."""
    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", _fake_provider({}))
    recorder = _SpyRecorder()
    context = _context()
    context.decisions = [
        Decision("a.py", "vector", 0.65, None, True, selection.KEPT, "matched the request at 0.650"),
        Decision("noise.py", "vector", 0.05, None, False, selection.BELOW_MIN_SIMILARITY, "too low"),
    ]

    scheduler.run_task(repo_root, "backend", "x", context, recorder=recorder)

    reason = json.loads(recorder.calls[0]["context_reason"])
    assert [k["path"] for k in reason["kept"]] == ["a.py"]
    assert reason["dropped"] == {selection.BELOW_MIN_SIMILARITY: 1}


def test_run_task_records_budget_drops_separately_from_relevance_drops(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> None:
    """Two different facts with two different fixes: raise the budget, or
    lower the floor."""
    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", _fake_provider({}))
    recorder = _SpyRecorder()
    context = SelectedContext(
        chunks=[
            {"path": f"f{i}.py", "kind": "function", "name": "foo",
             "start_line": 1, "end_line": 2, "text": "x" * 500}
            for i in range(4)
        ],
        injection_mode=FULL,
        max_context_chars=1200,
        decisions=[
            Decision(f"f{i}.py", "vector", 0.6, None, True, selection.KEPT, "matched") for i in range(4)
        ],
    )

    scheduler.run_task(repo_root, "backend", "x", context, recorder=recorder)

    reason = json.loads(recorder.calls[0]["context_reason"])
    assert reason["dropped"]["max_context_chars"] > 0
    assert recorder.calls[0]["context_files"] < 4


def test_run_task_without_context_records_no_reason(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> None:
    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", _fake_provider({}))
    recorder = _SpyRecorder()

    scheduler.run_task(repo_root, "backend", "x", recorder=recorder)

    assert recorder.calls[0]["context_reason"] == ""


def test_run_task_records_why_the_provider_was_chosen(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> None:
    """routing_reason has been in the schema and empty since step 1, on the
    principle that a decision is recorded as it happens or not at all."""
    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", _fake_provider({}))
    recorder = _SpyRecorder()

    scheduler.run_task(repo_root, "backend", "x", recorder=recorder)

    assert recorder.calls[0]["routing_reason"]


def test_routing_reads_history_from_engine_root_not_the_task_worktree(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path, tmp_path: Path
) -> None:
    """DAG stages run in a throwaway worktree. Routing off that path would
    decide from an empty database — every stage cold-starting forever — and
    would drop a stray telemetry.sqlite in the worktree for the stage's own
    commit to sweep up (it did, and the contract check caught it). Passing
    `engine_root` explicitly is what fixes it — it's never derived from
    `repo_root`, worktree or not."""
    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", _fake_provider({}))
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    scheduler.run_task(worktree, "backend", "x", engine_root=repo_root)

    assert not (worktree / "telemetry.sqlite").exists()
    assert (repo_root / "telemetry.sqlite").exists()


def test_a_failed_call_records_why_it_failed(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    """Recording that something failed without recording why is the difference
    between measuring a failure rate and being able to act on one — a real run
    failed and the table could say nothing about the cause."""

    def failing_run(task: AgentTask) -> ProviderResult:
        return ProviderResult(success=False, summary="claude CLI failed (code 1): boom")

    monkeypatch.setitem(
        scheduler.PROVIDERS, "claude_code", type("FakeProvider", (), {"run": staticmethod(failing_run)})
    )
    recorder = _SpyRecorder()

    scheduler.run_task(repo_root, "backend", "x", recorder=recorder)

    assert recorder.calls[0]["metadata"]["error"] == "claude CLI failed (code 1): boom"


def test_a_successful_call_records_no_error(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", _fake_provider({}))
    recorder = _SpyRecorder()

    scheduler.run_task(repo_root, "backend", "x", recorder=recorder)

    assert "error" not in recorder.calls[0]["metadata"]




def test_call_metadata_records_the_effective_model_reported_by_provider(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> None:
    def fake_run(task: AgentTask) -> ProviderResult:
        return ProviderResult(
            success=True,
            summary="done",
            usage=TokenUsage(model="claude-opus-4-8"),
        )

    monkeypatch.setitem(
        scheduler.PROVIDERS,
        "claude_code",
        type("FakeProvider", (), {"run": staticmethod(fake_run)}),
    )
    recorder = _SpyRecorder()

    scheduler.run_task(repo_root, "backend", "x", recorder=recorder)

    assert recorder.calls[0]["metadata"]["effective_model"] == "claude-opus-4-8"


def test_a_runaway_error_message_is_truncated(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    """A provider dumping megabytes of stderr must not bloat the row."""

    def failing_run(task: AgentTask) -> ProviderResult:
        return ProviderResult(success=False, summary="x" * 100_000)

    monkeypatch.setitem(
        scheduler.PROVIDERS, "claude_code", type("FakeProvider", (), {"run": staticmethod(failing_run)})
    )
    recorder = _SpyRecorder()

    scheduler.run_task(repo_root, "backend", "x", recorder=recorder)

    assert len(recorder.calls[0]["metadata"]["error"]) == scheduler.MAX_ERROR_CHARS
