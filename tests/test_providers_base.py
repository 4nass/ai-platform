"""Tests for providers.base."""

from __future__ import annotations

from pathlib import Path

from providers.base import AgentTask, ProviderResult, display_name, load_role_prompt


def test_load_role_prompt_existing_file(tmp_path: Path) -> None:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "backend.md").write_text("You are the Backend Agent.", encoding="utf-8")

    assert load_role_prompt(tmp_path, "backend") == "You are the Backend Agent."


def test_load_role_prompt_missing_file_returns_empty_string(tmp_path: Path) -> None:
    assert load_role_prompt(tmp_path, "does_not_exist") == ""


def test_display_name_known_provider() -> None:
    assert display_name("claude_code") == "Claude Code"


def test_display_name_unknown_provider_falls_back_to_raw_name() -> None:
    assert display_name("some_new_provider") == "some_new_provider"


def test_agent_task_defaults() -> None:
    task = AgentTask(agent="backend", description="do x", repo_root=Path("."))

    assert task.context_paths == []
    assert task.context_render == ""


def test_provider_result_defaults() -> None:
    result = ProviderResult(success=True, summary="done")

    assert result.raw is None
