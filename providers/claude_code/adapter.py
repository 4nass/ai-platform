"""Provider Claude Code CLI : Hermes ne parle pas au modèle, il pilote le CLI.

`claude -p` s'exécute en mode non-interactif, authentifié via la session
d'abonnement déjà active (`claude auth login`) — pas de clé API. Le CLI édite
lui-même les fichiers (Read/Edit/Write) ; ce provider ne fait qu'invoquer le
process et relayer son résumé, conformément au contrat providers.base.Provider.

Flags vérifiés via la documentation Claude Code, et la forme du JSON de sortie
confirmée empiriquement (`result`, `is_error`, `subtype`, `session_id`,
`total_cost_usd`) — le binaire est présent dans ce sandbox mais non
authentifié, ce qui suffit à valider le parsing sans pouvoir tester une
véritable exécution de tâche.
"""

from __future__ import annotations

import json
import subprocess

from providers.base import AgentTask, ProviderResult, load_role_prompt

TIMEOUT_SECONDS = 900
ALLOWED_TOOLS = "Read,Edit,Write,Bash(uv run pytest*)"


def _build_prompt(task: AgentTask) -> str:
    if not task.context_paths:
        return task.description
    listing = "\n".join(f"- {p}" for p in task.context_paths)
    return f"{task.description}\n\nFichiers de contexte probablement pertinents (lis-les toi-même si besoin) :\n{listing}"


def run(task: AgentTask) -> ProviderResult:
    system_prompt = load_role_prompt(task.repo_root, task.agent)

    cmd = [
        "claude",
        "-p",
        _build_prompt(task),
        "--add-dir",
        str(task.repo_root),
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        ALLOWED_TOOLS,
        "--output-format",
        "json",
    ]
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]

    try:
        proc = subprocess.run(
            cmd,
            cwd=task.repo_root,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return ProviderResult(
            success=False,
            summary=(
                "Binaire `claude` introuvable dans le PATH. Installe le CLI Claude Code "
                "et lance `claude auth login` avant de relancer."
            ),
        )
    except subprocess.TimeoutExpired as exc:
        return ProviderResult(success=False, summary=f"claude CLI : timeout après {exc.timeout}s")

    # L'info d'erreur (ex. "Not logged in · Please run /login") arrive dans le
    # JSON sur stdout, pas sur stderr — on tente le parsing avant de se fier
    # au seul code de sortie.
    data: dict | None = None
    if proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout)
            data = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            data = None

    if proc.returncode != 0:
        if data and data.get("result"):
            return ProviderResult(
                success=False,
                summary=f"claude CLI (code {proc.returncode}) : {data['result']}",
                raw=data,
            )
        return ProviderResult(
            success=False,
            summary=f"claude CLI a échoué (code {proc.returncode}) : {proc.stderr.strip() or proc.stdout.strip()}",
            raw=proc.stderr or proc.stdout,
        )

    if data is None:
        return ProviderResult(
            success=False,
            summary="Sortie non-JSON du CLI claude — voir `raw` pour le contenu brut.",
            raw=proc.stdout,
        )

    if data.get("is_error"):
        return ProviderResult(success=False, summary=data.get("result", "Erreur inconnue"), raw=data)

    return ProviderResult(success=True, summary=data.get("result", ""), raw=data)
