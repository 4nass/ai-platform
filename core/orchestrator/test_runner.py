"""Exécution de la suite de tests du repo après modification."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TestResult:
    passed: bool
    output: str


def run_tests(repo_root: Path) -> TestResult:
    try:
        proc = subprocess.run(
            ["uv", "run", "pytest", "-q"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        return TestResult(passed=False, output=f"Timeout après {exc.timeout}s")
    return TestResult(passed=proc.returncode == 0, output=proc.stdout + proc.stderr)


def format_test_summary(result: TestResult) -> str:
    """Formate un `TestResult` en une ligne de résumé lisible (PASS/FAIL + sortie)."""
    status = "PASS" if result.passed else "FAIL"
    output = result.output.strip()
    return f"[{status}] {output}" if output else f"[{status}]"
