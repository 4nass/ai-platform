"""OpenAI API provider — not implemented.

Out of scope for now: the user drives work through subscription-based CLIs
(claude_code, eventually codex_cli), not separately billed APIs.
Implement if this need comes up later.
"""

from __future__ import annotations

from providers.base import AgentTask, ProviderResult


def run(task: AgentTask) -> ProviderResult:
    raise NotImplementedError(
        "openai_api provider not implemented (out of scope for now)."
    )
