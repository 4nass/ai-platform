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
    """The selected paths, most relevant first — the machine-readable form of
    the selection (counted in telemetry). The prose the model actually reads
    is `context_render`."""
    context_render: str = ""
    """The selected context, already rendered for *this* provider's shape by
    core.context.manager.SelectedContext.render_for — a ranked map for a
    provider that reads files itself, the full excerpt text for one that
    can't. Adapters inject it as given; they don't decide what goes in it."""


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
    READS_FILES: bool
    """Whether this provider can read the repo itself.

    Declared by the adapter, which is the only place that knows — same
    separation as usage parsing. It decides how much context is worth putting
    in the prompt: pointing a provider at files it can open beats sending it
    excerpts, but a provider with no disk access sees only its prompt, so for
    it the content is the context.
    """

    def run(self, task: AgentTask) -> ProviderResult: ...


def reads_files(provider: object) -> bool:
    """Conservative default: a provider that doesn't declare itself is assumed
    to have no disk access, so it gets the full content rather than a map of
    paths it may not be able to open."""
    return bool(getattr(provider, "READS_FILES", False))


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
