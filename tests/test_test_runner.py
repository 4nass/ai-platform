"""Tests for core.orchestrator.test_runner."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from core.orchestrator import test_runner
from core.orchestrator.target_config import TargetConfig
from core.orchestrator.test_runner import TestResult, format_test_summary

BWRAP_MISSING = shutil.which("bwrap") is None


def _config(command="uv run pytest -q", *, sandbox: bool = False, **kwargs) -> TargetConfig:
    """The frozen policy the supervisor would have read from the base commit.

    Built directly rather than written to disk: `run_tests` no longer reads
    `.ai-platform.yml` at all, which is the point of the fix — a stage that
    rewrites that file mid-run must not change the policy the run is judged
    under. Sandbox defaults off here so the parsing and pass/fail tests
    behave the same whether or not `bwrap` is installed on the machine
    running the suite; sandboxing has its own tests below.
    """
    if isinstance(command, str):
        command = tuple(command.split())
    return TargetConfig(test_command=command, test_sandbox=sandbox, **kwargs)


def _fake_run(captured: dict, *, returncode: int = 0, stdout: str = "", stderr: str = ""):
    def run(cmd, cwd, capture_output, text, timeout, env=None):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, returncode=returncode, stdout=stdout, stderr=stderr)

    return run


def test_format_test_summary_passed_with_output() -> None:
    assert format_test_summary(TestResult(passed=True, output="3 passed in 0.01s")) == "[PASS] 3 passed in 0.01s"


def test_format_test_summary_failed_with_output() -> None:
    assert format_test_summary(TestResult(passed=False, output="1 failed, 2 passed")) == "[FAIL] 1 failed, 2 passed"


def test_format_test_summary_without_output() -> None:
    assert format_test_summary(TestResult(passed=True, output="")) == "[PASS]"


def test_format_test_summary_strips_whitespace() -> None:
    assert format_test_summary(TestResult(passed=False, output="  error  \n")) == "[FAIL] error"


def test_format_test_summary_skipped() -> None:
    result = TestResult(passed=True, skipped=True, output="No test_command declared")
    assert format_test_summary(result) == "[SKIPPED] No test_command declared"


def test_run_tests_skips_cleanly_when_no_test_command_declared(tmp_path: Path) -> None:
    result = test_runner.run_tests(tmp_path, TargetConfig())

    assert result.skipped is True
    assert result.passed is True
    assert "test_command" in result.output


def test_run_tests_reports_pass(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}
    monkeypatch.setattr(test_runner.subprocess, "run", _fake_run(captured, stdout="3 passed\n"))

    result = test_runner.run_tests(tmp_path, _config())

    assert result.passed is True
    assert result.skipped is False
    assert result.sandboxed is False
    assert "3 passed" in result.output
    assert captured["cmd"] == ["uv", "run", "pytest", "-q"]


def test_run_tests_reports_failure_with_combined_output(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        test_runner.subprocess, "run", _fake_run({}, returncode=1, stdout="1 failed\n", stderr="traceback...")
    )

    result = test_runner.run_tests(tmp_path, _config())

    assert result.passed is False
    assert "1 failed" in result.output
    assert "traceback..." in result.output


def test_run_tests_handles_timeout(monkeypatch, tmp_path: Path) -> None:
    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=120)

    monkeypatch.setattr(test_runner.subprocess, "run", raise_timeout)

    result = test_runner.run_tests(tmp_path, _config())

    assert result.passed is False
    assert "Timeout" in result.output


def test_run_tests_honours_a_list_command_and_custom_timeout(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}
    monkeypatch.setattr(test_runner.subprocess, "run", _fake_run(captured))

    result = test_runner.run_tests(tmp_path, _config(("npm", "test"), test_timeout=30))

    assert result.passed is True
    assert captured["cmd"] == ["npm", "test"]
    assert captured["timeout"] == 30


def test_run_tests_applies_test_env_when_unsandboxed(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}
    monkeypatch.setattr(test_runner.subprocess, "run", _fake_run(captured))

    test_runner.run_tests(tmp_path, _config("pytest", test_env=(("HF_HUB_OFFLINE", "1"),)))

    assert captured["env"]["HF_HUB_OFFLINE"] == "1"
    assert "PATH" in captured["env"]  # merged with the parent environment, not replacing it


# --- sandboxing (issue #4) ---


def test_run_tests_sandboxes_by_default_when_bwrap_is_available(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(test_runner.shutil, "which", lambda name: "/usr/bin/bwrap")
    captured: dict = {}
    monkeypatch.setattr(test_runner.subprocess, "run", _fake_run(captured))

    result = test_runner.run_tests(tmp_path, _config(sandbox=True))

    assert result.sandboxed is True
    assert result.sandbox_warning == ""
    assert captured["cmd"][0] == "bwrap"
    assert captured["cmd"][-4:] == ["uv", "run", "pytest", "-q"]


def test_run_tests_falls_back_loudly_when_bwrap_is_missing(monkeypatch, tmp_path: Path) -> None:
    """Degrading loudly beats silently running unprotected -- same reasoning
    as the router's never-block guarantee: tests still run, but the report
    says plainly that they ran without isolation and why."""
    monkeypatch.setattr(test_runner.shutil, "which", lambda name: None)
    captured: dict = {}
    monkeypatch.setattr(test_runner.subprocess, "run", _fake_run(captured))

    result = test_runner.run_tests(tmp_path, _config(sandbox=True))

    assert result.sandboxed is False
    assert "bwrap" in result.sandbox_warning
    assert captured["cmd"] == ["uv", "run", "pytest", "-q"]


def test_run_tests_respects_an_explicit_sandbox_opt_out(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(test_runner.shutil, "which", lambda name: "/usr/bin/bwrap")
    captured: dict = {}
    monkeypatch.setattr(test_runner.subprocess, "run", _fake_run(captured))

    result = test_runner.run_tests(tmp_path, _config(sandbox=False))

    assert result.sandboxed is False
    assert result.sandbox_warning == ""  # opted out, not degraded -- no warning needed
    assert captured["cmd"] == ["uv", "run", "pytest", "-q"]


def test_sandbox_command_shape(tmp_path: Path) -> None:
    cmd = test_runner._sandbox_command(tmp_path, ["~/.cache"], {"FOO": "bar"})

    assert cmd[0] == "bwrap"
    assert "--unshare-all" in cmd
    assert "--die-with-parent" in cmd
    ro_idx = cmd.index("--ro-bind")
    assert cmd[ro_idx + 1 : ro_idx + 3] == ["/", "/"]
    bind_idx = cmd.index("--bind")
    assert cmd[bind_idx + 1 : bind_idx + 3] == [str(tmp_path), str(tmp_path)]
    assert "--setenv" in cmd and "FOO" in cmd and "bar" in cmd
    assert cmd[-1] == "--"


@pytest.mark.skipif(BWRAP_MISSING, reason="bwrap not installed on this machine")
def test_sandbox_blocks_writes_outside_the_repo_for_real(tmp_path: Path) -> None:
    """Live integration test against the real bwrap binary, not a mock --
    proves the isolation actually holds rather than just that the right
    flags were assembled.

    The canary deliberately lives under $HOME, not under tmp_path's parent:
    /tmp is *itself* a fresh, fully-writable tmpfs inside the sandbox (see
    _sandbox_command) so a stage can still use scratch space there -- a
    canary anywhere under /tmp would only prove a write landed in that
    private, ephemeral copy, never touching the real host path at all, which
    is exactly the false pass this test caught on the first attempt."""
    outside = Path.home() / f"ai-platform-sandbox-canary-{tmp_path.name}.txt"
    outside.unlink(missing_ok=True)
    config = _config(("python3", "-c", f"open({str(outside)!r}, 'w').write('pwned')"), sandbox=True)

    try:
        result = test_runner.run_tests(tmp_path, config)

        assert result.sandboxed is True
        assert result.passed is False  # the write raised inside the sandboxed process
        assert not outside.exists()
    finally:
        outside.unlink(missing_ok=True)


@pytest.mark.skipif(BWRAP_MISSING, reason="bwrap not installed on this machine")
def test_sandbox_blocks_network_for_real(tmp_path: Path) -> None:
    script = (
        "import socket, sys\n"
        "try:\n"
        "    socket.create_connection(('8.8.8.8', 53), timeout=3)\n"
        "    sys.exit(1)\n"
        "except OSError:\n"
        "    sys.exit(0)\n"
    )

    result = test_runner.run_tests(tmp_path, _config(("python3", "-c", script), sandbox=True))

    assert result.sandboxed is True
    assert result.passed is True  # exit 0 only on the branch where the connection failed


@pytest.mark.skipif(BWRAP_MISSING, reason="bwrap not installed on this machine")
def test_sandbox_still_allows_writes_inside_the_repo(tmp_path: Path) -> None:
    config = _config(("python3", "-c", "open('inside.txt', 'w').write('ok')"), sandbox=True)

    result = test_runner.run_tests(tmp_path, config)

    assert result.passed is True
    assert (tmp_path / "inside.txt").read_text(encoding="utf-8") == "ok"
