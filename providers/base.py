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
class ProviderResult:
    success: bool
    summary: str
    raw: object = None


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
