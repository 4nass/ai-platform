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


def test_every_adapter_declares_whether_it_reads_files() -> None:
    """The scheduler renders context per provider shape; an adapter that
    forgets to declare falls back to 'no disk access', which sends content
    rather than a map of paths it might not be able to open."""
    from providers.anthropic_api import adapter as anthropic_api
    from providers.claude_code import adapter as claude_code
    from providers.codex_cli import adapter as codex_cli
    from providers.openai_api import adapter as openai_api

    assert (claude_code.READS_FILES, codex_cli.READS_FILES) == (True, True)
    assert (anthropic_api.READS_FILES, openai_api.READS_FILES) == (False, False)


def test_the_two_cli_providers_normalize_into_the_same_token_convention() -> None:
    """The convention (see TokenUsage) is that input + cache_read +
    cache_creation is the true prompt size. Anthropic already splits them that
    way; OpenAI counts the cached portion inside its input figure. Both
    adapters must land on the same meaning, or totals across providers are
    quietly incomparable — which is exactly what step 5 would route on.
    """
    from providers.claude_code import adapter as claude_code
    from providers.codex_cli import adapter as codex_cli

    # Same underlying call: a 1,000-token prompt, 900 of it served from cache.
    anthropic_shape = claude_code._parse_usage(
        {"usage": {"input_tokens": 100, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0}}
    )
    openai_shape = codex_cli._parse_usage(
        {"input_tokens": 1000, "cached_input_tokens": 900, "cache_write_input_tokens": 0}
    )

    def prompt_size(usage) -> int:
        return usage.input_tokens + usage.cache_read_tokens + usage.cache_creation_tokens

    assert prompt_size(anthropic_shape) == prompt_size(openai_shape) == 1000
    assert anthropic_shape.input_tokens == openai_shape.input_tokens == 100
