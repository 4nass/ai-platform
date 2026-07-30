"""Codex CLI provider — not implemented.

The exact Codex CLI flags (non-interactive invocation, passing context,
restricting tools, output format) couldn't be verified (binary absent from
this environment, no reference agent equivalent to claude-code-guide for
Codex). Explicit decision: don't guess this interface — implement this
provider only once the real syntax is confirmed (e.g. `codex --help`
output provided by the user, or manual testing).
"""

from __future__ import annotations

from providers.base import AgentTask, ProviderResult


def run(task: AgentTask) -> ProviderResult:
    raise NotImplementedError(
        "codex_cli provider not implemented: the Codex CLI syntax hasn't been "
        "verified. Provide `codex --help` (or equivalent) to implement it."
    )
