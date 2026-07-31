"""Tests for core.orchestrator.contracts."""

from __future__ import annotations

from core.orchestrator.contracts import violations


def test_architect_allowed_paths_pass() -> None:
    files = ["memory/architecture.md", "memory/adr/ADR-004-oauth.md"]

    assert violations("architect", files) == []


def test_architect_touching_application_code_is_flagged() -> None:
    files = ["memory/architecture.md", "core/auth/oauth.py"]

    assert violations("architect", files) == ["core/auth/oauth.py"]


def test_documentation_allowed_paths_pass() -> None:
    files = ["README.md", "memory/roadmap.md", "memory/adr/ADR-004-oauth.md"]

    assert violations("documentation", files) == []


def test_documentation_touching_application_code_is_flagged() -> None:
    files = ["README.md", "core/auth/oauth.py"]

    assert violations("documentation", files) == ["core/auth/oauth.py"]


def test_security_may_not_touch_any_file() -> None:
    assert violations("security", ["README.md"]) == ["README.md"]


def test_security_with_no_files_changed_is_compliant() -> None:
    assert violations("security", []) == []


def test_role_with_no_declared_contract_is_unrestricted() -> None:
    assert violations("backend", ["core/anything.py", "tests/test_anything.py"]) == []
