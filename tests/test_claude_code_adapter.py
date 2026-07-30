"""Tests for providers.claude_code.adapter."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from providers.base import AgentTask
from providers.claude_code import adapter


def _task(agent: str = "backend", context_paths: list[str] | None = None) -> AgentTask:
    return AgentTask(agent=agent, description="do x", repo_root=Path("."), context_paths=context_paths or [])


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


def test_build_prompt_without_context_paths() -> None:
    assert adapter._build_prompt(_task()) == "do x"


def test_build_prompt_lists_context_paths() -> None:
    prompt = adapter._build_prompt(_task(context_paths=["a.py", "b.py"]))

    assert "a.py" in prompt
    assert "b.py" in prompt
    assert "do x" in prompt


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
