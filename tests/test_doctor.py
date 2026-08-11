"""Tests for the read-only doctor diagnostics."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import ai_platform
from core import doctor

runner = CliRunner()


def test_report_is_failed_only_when_a_check_fails() -> None:
    assert not doctor.Report((doctor.Check("x", "PASS", "ok"),)).failed
    assert doctor.Report((doctor.Check("x", "WARN", "degraded"),)).failed is False
    assert doctor.Report((doctor.Check("x", "FAIL", "broken"),)).failed


def test_missing_required_tool_is_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)

    check = doctor._tool_check("uv")

    assert check.status == "FAIL"
    assert "PATH" in check.detail


def test_uv_outside_path_prints_exact_bash_fix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    uv = tmp_path / "uv"
    uv.touch()
    uv.chmod(0o755)
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    monkeypatch.setattr(doctor, "_uv_candidates", lambda: (uv,))

    check = doctor._tool_check("uv")

    assert check.status == "FAIL"
    assert f"installed at {uv}" in check.detail
    assert f'export PATH="{tmp_path}:$PATH"' in check.remediation
    assert "source ~/.bashrc" in check.remediation


def test_uv_missing_prints_install_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    monkeypatch.setattr(doctor, "_uv_candidates", lambda: ())

    check = doctor._tool_check("uv")

    assert check.status == "FAIL"
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in check.remediation


def test_missing_optional_tool_is_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)

    check = doctor._tool_check("bwrap", required=False)

    assert check.status == "WARN"


def _fake_platform():
    return SimpleNamespace(profile="balanced")


def test_one_authenticated_provider_keeps_provider_gate_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    from providers.codex_cli import adapter as codex

    from core.orchestrator import platform_config, router

    monkeypatch.setattr(platform_config, "load", lambda *_: _fake_platform())
    monkeypatch.setattr(router, "eligible_profiles", lambda *args, **kwargs: [router.ExecutionProfile("codex_cli"), router.ExecutionProfile("claude_code")])
    monkeypatch.setattr(doctor, "_profile_roles", lambda *_: {"backend"})
    monkeypatch.setattr(doctor.shutil, "which", lambda command: None if command == "claude" else f"/usr/bin/{command}")
    monkeypatch.setattr(codex, "_check_auth", lambda: None)

    checks = doctor._provider_checks(Path("/engine"))

    assert any(check.name == "Provider claude_code" and check.status == "WARN" for check in checks)
    assert any(check.name == "Provider codex_cli" and check.status == "PASS" for check in checks)
    assert any(check.name == "At least one provider" and check.status == "PASS" for check in checks)


def test_no_authenticated_provider_is_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.orchestrator import platform_config, router

    monkeypatch.setattr(platform_config, "load", lambda *_: _fake_platform())
    monkeypatch.setattr(router, "eligible_profiles", lambda *args, **kwargs: [router.ExecutionProfile("codex_cli")])
    monkeypatch.setattr(doctor, "_profile_roles", lambda *_: {"backend"})
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)

    checks = doctor._provider_checks(Path("/engine"))

    aggregate = next(check for check in checks if check.name == "At least one provider")
    assert aggregate.status == "FAIL"


def test_doctor_cli_prints_statuses_and_fails_on_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ai_platform, "_admit", lambda *args, **kwargs: (tmp_path, None))
    monkeypatch.setattr(
        "core.doctor.run",
        lambda *args, **kwargs: doctor.Report(
            (
                doctor.Check("good", "PASS", "ready"),
                doctor.Check("optional", "WARN", "degraded"),
                doctor.Check("blocked", "FAIL", "missing"),
            )
        ),
    )

    result = runner.invoke(ai_platform.app, ["doctor", "--repo", str(tmp_path)])

    assert result.exit_code == 1
    assert "PASS" in result.stdout
    assert "WARN" in result.stdout
    assert "FAIL" in result.stdout
