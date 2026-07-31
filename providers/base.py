"""Common interface for all providers (CLI or API).

Contract: by the time `run()` returns, the disk is already up to date —
however that happens (a CLI provider edits files itself via its own tools;
an API provider must write the files it receives before returning). This
keeps the orchestrator agnostic to which provider is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

PROMPTS_DIR = Path("prompts")


@dataclass
class AgentTask:
    agent: str
    description: str
    repo_root: Path
    context_paths: list[str] = field(default_factory=list)
    context_render: str = ""
    """Full context (file content, git diff, memory) for API providers with
    no disk access. CLI providers use `context_paths` instead (just the
    paths — they read the files themselves)."""


@dataclass
class TokenUsage:
    """What one provider call consumed, normalized across providers.

    Each adapter maps its own wire format into this — the orchestrator and
    core.telemetry never learn a provider's response shape. Every field
    defaults, so a provider that reports nothing (or whose payload changed
    shape) yields a usable record instead of breaking a run.
    """

    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float | None = None
    """None means the provider reported no cost — distinct from 0.0, which
    would claim the call was free. Pricing an unpriced call means hardcoding
    a rate table that goes stale silently; that belongs at query time."""
    provider_duration_ms: int | None = None
    """The provider's own timing, when it reports one. The orchestrator
    measures wall clock separately; the gap between the two is our overhead."""


@dataclass
class ProviderResult:
    success: bool
    summary: str
    raw: object = None
    usage: TokenUsage | None = None


class Provider(Protocol):
    def run(self, task: AgentTask) -> ProviderResult: ...


def load_role_prompt(repo_root: Path, agent: str) -> str:
    path = repo_root / PROMPTS_DIR / f"{agent}.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


PROVIDER_DISPLAY_NAMES = {
    "claude_code": "Claude Code",
    "codex_cli": "Codex CLI",
    "anthropic_api": "Anthropic API",
    "openai_api": "OpenAI API",
}


def display_name(provider_name: str) -> str:
    return PROVIDER_DISPLAY_NAMES.get(provider_name, provider_name)
