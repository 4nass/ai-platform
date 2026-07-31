"""Runs the target repo's test suite after a modification.

The command is declared by the target repo itself (.ai-platform.yml), not
assumed: "uv run pytest -q" is only true for this repo, and a `--repo`
target using npm, go test, cargo, etc. would otherwise get a meaningless
failure against a command that doesn't even apply to it.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_PATH = Path(".ai-platform.yml")
DEFAULT_TIMEOUT_SECONDS = 120


@dataclass
class TestResult:
    passed: bool
    output: str
    skipped: bool = False
    """True when the target declared no test_command. `passed` is still True
    in that case — skipped is neutral, not a failure — but kept as a
    distinct field so a caller can tell "nothing to fail" from "verified and
    green"."""


def _load_test_command(repo_root: Path) -> tuple[list[str] | None, int]:
    path = repo_root / CONFIG_PATH
    if not path.is_file():
        return None, DEFAULT_TIMEOUT_SECONDS

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    command = data.get("test_command")
    timeout = data.get("test_timeout", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        timeout = DEFAULT_TIMEOUT_SECONDS

    if isinstance(command, str):
        command = shlex.split(command)
    elif isinstance(command, list) and all(isinstance(part, str) for part in command):
        command = list(command)
    else:
        command = None
    return command, timeout


def run_tests(repo_root: Path) -> TestResult:
    command, timeout = _load_test_command(repo_root)
    if command is None:
        return TestResult(
            passed=True,
            skipped=True,
            output=f"No test_command declared in {CONFIG_PATH} — tests skipped.",
        )

    try:
        proc = subprocess.run(command, cwd=repo_root, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return TestResult(passed=False, output=f"Timeout after {exc.timeout}s")
    except FileNotFoundError as exc:
        return TestResult(passed=False, output=f"test_command not found: {exc}")
    return TestResult(passed=proc.returncode == 0, output=proc.stdout + proc.stderr)


def format_test_summary(result: TestResult) -> str:
    """Formats a `TestResult` into a readable one-line summary (PASS/FAIL/SKIPPED + output)."""
    status = "SKIPPED" if result.skipped else ("PASS" if result.passed else "FAIL")
    output = result.output.strip()
    return f"[{status}] {output}" if output else f"[{status}]"
