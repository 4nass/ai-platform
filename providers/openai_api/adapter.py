"""Provider API OpenAI — non implémenté.

Hors scope pour l'instant : l'utilisateur pilote via des CLI sur abonnement
(claude_code, à terme codex_cli), pas via des API facturées séparément.
À implémenter si ce besoin apparaît plus tard.
"""

from __future__ import annotations

from providers.base import AgentTask, ProviderResult


def run(task: AgentTask) -> ProviderResult:
    raise NotImplementedError(
        "Provider openai_api non implémenté (hors scope pour l'instant)."
    )
