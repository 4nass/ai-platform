"""Tests for core.orchestrator.test_runner."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from core.orchestrator import test_runner
from core.orchestrator.test_runner import TestResult, format_test_summary

BWRAP_MISSING = shutil.which("bwrap") is None


def _declare_test_command(
    tmp_path: Path, command: str = "uv run pytest -q", *, sandbox: bool | None = False
) -> None:
    """Sandbox defaults to explicitly disabled here: most of these tests are
    about command parsing/pass-fail/timeout reporting, which should behave
    identically whether or not `bwrap` happens to be installed on whatever
    machine runs this suite. Sandboxing itself gets its own tests below."""
    lines = [f"test_command: {command!r}"]
    if sandbox is not None:
        lines.append(f"test_sandbox: {'true' if sandbox else 'false'}")
    (tmp_path / test_runner.CONFIG_PATH).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_format_test_summary_passed_with_output() -> None:
    result = TestResult(passed=True, output="3 passed in 0.01s")
    assert format_test_summary(result) == "[PASS] 3 passed in 0.01s"


def test_format_test_summary_failed_with_output() -> None:
    result = TestResult(passed=False, output="1 failed, 2 passed")
    assert format_test_summary(result) == "[FAIL] 1 failed, 2 passed"


def test_format_test_summary_without_output() -> None:
    result = TestResult(passed=True, output="")
    assert format_test_summary(result) == "[PASS]"


def test_format_test_summary_strips_whitespace() -> None:
    result = TestResult(passed=False, output="  error  \n")
    assert format_test_summary(result) == "[FAIL] error"


def test_format_test_summary_skipped() -> None:
    result = TestResult(passed=True, skipped=True, output="No test_command declared")
    assert format_test_summary(result) == "[SKIPPED] No test_command declared"


def test_run_tests_skips_cleanly_when_no_config_declared(tmp_path: Path) -> None:
    result = test_runner.run_tests(tmp_path)

    assert result.skipped is True
    assert result.passed is True
    assert "test_command" in result.output


def test_run_tests_reports_pass(monkeypatch, tmp_path: Path) -> None:
    _declare_test_command(tmp_path)

    def fake_run(cmd, cwd, capture_output, text, timeout):
        assert cmd == ["uv", "run", "pytest", "-q"]
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="3 passed\n", stderr="")

    monkeypatch.setattr(test_runner.subprocess, "run", fake_run)

    result = test_runner.run_tests(tmp_path)

    assert result.passed is True
    assert result.skipped is False
    assert result.sandboxed is False
    assert "3 passed" in result.output


def test_run_tests_reports_failure_with_combined_output(monkeypatch, tmp_path: Path) -> None:
    _declare_test_command(tmp_path)

    def fake_run(cmd, cwd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="1 failed\n", stderr="traceback...")

    monkeypatch.setattr(test_runner.subprocess, "run", fake_run)

    result = test_runner.run_tests(tmp_path)

    assert result.passed is False
    assert "1 failed" in result.output
    assert "traceback..." in result.output


def test_run_tests_handles_timeout(monkeypatch, tmp_path: Path) -> None:
    _declare_test_command(tmp_path)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=120)

    monkeypatch.setattr(test_runner.subprocess, "run", fake_run)

    result = test_runner.run_tests(tmp_path)

    assert result.passed is False
    assert "Timeout" in result.output


def test_run_tests_reads_a_list_form_command(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / test_runner.CONFIG_PATH).write_text(
        "test_command: [npm, test]\ntest_timeout: 30\ntest_sandbox: false\n", encoding="utf-8"
    )

    captured = {}

    def fake_run(cmd, cwd, capture_output, text, timeout):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(test_runner.subprocess, "run", fake_run)

    result = test_runner.run_tests(tmp_path)

    assert result.passed is True
    assert captured["cmd"] == ["npm", "test"]
    assert captured["timeout"] == 30


def test_run_tests_applies_test_env_when_unsandboxed(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / test_runner.CONFIG_PATH).write_text(
        "test_command: pytest\ntest_sandbox: false\ntest_env:\n  HF_HUB_OFFLINE: '1'\n",
        encoding="utf-8",
    )
    captured = {}

    def fake_run(cmd, cwd, capture_output, text, timeout, env=None):
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(test_runner.subprocess, "run", fake_run)

    test_runner.run_tests(tmp_path)

    assert captured["env"]["HF_HUB_OFFLINE"] == "1"
    assert "PATH" in captured["env"]  # merged with the parent environment, not replacing it


# --- sandboxing (issue #4) ---


def test_run_tests_sandboxes_by_default_when_bwrap_is_available(monkeypatch, tmp_path: Path) -> None:
    _declare_test_command(tmp_path, sandbox=None)  # no explicit test_sandbox key -- true is the default
    monkeypatch.setattr(test_runner.shutil, "which", lambda name: "/usr/bin/bwrap")

    captured = {}

    def fake_run(cmd, cwd, capture_output, text, timeout):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(test_runner.subprocess, "run", fake_run)

    result = test_runner.run_tests(tmp_path)

    assert result.sandboxed is True
    assert result.sandbox_warning == ""
    assert captured["cmd"][0] == "bwrap"
    assert captured["cmd"][-4:] == ["uv", "run", "pytest", "-q"]


def test_run_tests_falls_back_loudly_when_bwrap_is_missing(monkeypatch, tmp_path: Path) -> None:
    """Degrading loudly beats silently running unprotected -- same reasoning
    as the router's never-block guarantee: tests still run, but the report
    says plainly that they ran without isolation and why."""
    _declare_test_command(tmp_path, sandbox=None)
    monkeypatch.setattr(test_runner.shutil, "which", lambda name: None)

    captured = {}

    def fake_run(cmd, cwd, capture_output, text, timeout):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(test_runner.subprocess, "run", fake_run)

    result = test_runner.run_tests(tmp_path)

    assert result.sandboxed is False
    assert "bwrap" in result.sandbox_warning
    assert captured["cmd"] == ["uv", "run", "pytest", "-q"]


def test_run_tests_respects_an_explicit_sandbox_opt_out(monkeypatch, tmp_path: Path) -> None:
    _declare_test_command(tmp_path, sandbox=False)
    monkeypatch.setattr(test_runner.shutil, "which", lambda name: "/usr/bin/bwrap")

    captured = {}

    def fake_run(cmd, cwd, capture_output, text, timeout):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(test_runner.subprocess, "run", fake_run)

    result = test_runner.run_tests(tmp_path)

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
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / test_runner.CONFIG_PATH).write_text(
        f"test_command: [\"python3\", \"-c\", \"open('{outside}', 'w').write('pwned')\"]\n",
        encoding="utf-8",
    )

    try:
        result = test_runner.run_tests(repo)

        assert result.sandboxed is True
        assert result.passed is False  # the write raised inside the sandboxed process
        assert not outside.exists()
    finally:
        outside.unlink(missing_ok=True)


@pytest.mark.skipif(BWRAP_MISSING, reason="bwrap not installed on this machine")
def test_sandbox_blocks_network_for_real(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / test_runner.CONFIG_PATH).write_text(
        "test_command:\n"
        "  - python3\n"
        "  - -c\n"
        "  - |\n"
        "    import socket, sys\n"
        "    try:\n"
        "        socket.create_connection(('8.8.8.8', 53), timeout=3)\n"
        "        sys.exit(1)\n"
        "    except OSError:\n"
        "        sys.exit(0)\n",
        encoding="utf-8",
    )

    result = test_runner.run_tests(repo)

    assert result.sandboxed is True
    assert result.passed is True  # exit 0 only on the branch where the connection failed


@pytest.mark.skipif(BWRAP_MISSING, reason="bwrap not installed on this machine")
def test_sandbox_still_allows_writes_inside_the_repo(tmp_path: Path) -> None:
    (tmp_path / test_runner.CONFIG_PATH).write_text(
        "test_command: [\"python3\", \"-c\", \"open('inside.txt', 'w').write('ok')\"]\n",
        encoding="utf-8",
    )

    result = test_runner.run_tests(tmp_path)

    assert result.passed is True
    assert (tmp_path / "inside.txt").read_text(encoding="utf-8") == "ok"
