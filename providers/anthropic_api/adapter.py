"""Direct Anthropic API provider (client.messages.parse).

Unlike the claude_code provider (CLI), this provider has no disk access at
all: it receives a structured modification plan (validated by Pydantic) and
must write the files itself before returning, to honor the shared
providers.base.Provider contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import anthropic
import yaml
from pydantic import BaseModel

from core.errors import ConfigError
from providers.base import AgentTask, ProviderResult, load_role_prompt

MODELS_CONFIG_PATH = Path("config/models.yaml")
TOKEN_BUDGET_CONFIG_PATH = Path("config/token_budget.yaml")


class FileChange(BaseModel):
    path: str
    action: Literal["create", "modify"]
    content: str


class CodeChangePlan(BaseModel):
    summary: str
    files: list[FileChange]


def _load_yaml(repo_root: Path, path: Path) -> dict:
    return yaml.safe_load((repo_root / path).read_text(encoding="utf-8")) or {}


def _resolve_inside_repo(repo_root: Path, candidate: str) -> Path:
    """Resolves `candidate` under `repo_root`, refusing anything that escapes it.

    The paths here come straight from the model's structured output, so they
    are untrusted input. Two ways out of the repo have to be closed, and
    `Path.__truediv__` silently allows both: a traversal (`../../etc/passwd`)
    and — less obviously — an absolute path, which *replaces* the base
    entirely (`Path("/repo") / "/etc/passwd"` is `/etc/passwd`). `.resolve()`
    also collapses symlinks, closing the symlink-escape variant.
    """
    root = repo_root.resolve()
    target = (root / candidate).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"refusing to write outside the repo: {candidate!r}")
    return target


def _write_files(repo_root: Path, files: list[FileChange]) -> list[str]:
    # Resolve every path up front: a plan that tries to escape is rejected
    # whole, rather than half-applied before hitting the bad entry.
    targets = [(_resolve_inside_repo(repo_root, f.path), f) for f in files]

    written = []
    for target, file_change in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file_change.content, encoding="utf-8")
        written.append(file_change.path)
    return written


def _model_id(models_config: dict) -> str:
    model_id = (models_config.get("models") or {}).get("claude", {}).get("model")
    if not model_id:
        raise ConfigError(
            f"{MODELS_CONFIG_PATH} is missing models.claude.model — cannot resolve which "
            "Anthropic model to call."
        )
    return model_id


def run(task: AgentTask) -> ProviderResult:
    models_config = _load_yaml(task.repo_root, MODELS_CONFIG_PATH)
    token_budget = _load_yaml(task.repo_root, TOKEN_BUDGET_CONFIG_PATH)

    model_id = _model_id(models_config)
    max_tokens = token_budget.get(task.agent, 10000)
    system_prompt = load_role_prompt(task.repo_root, task.agent)

    context_note = f"\n\nContext:\n{task.context_render}" if task.context_render else ""
    user_prompt = f"Request:\n{task.description}{context_note}"

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=model_id,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
        output_format=CodeChangePlan,
    )
    plan = response.parsed_output
    if plan is None:
        return ProviderResult(
            success=False,
            summary="The Anthropic API returned a response that didn't match the expected structured output.",
        )
    try:
        _write_files(task.repo_root, plan.files)
    except ValueError as exc:
        return ProviderResult(success=False, summary=str(exc), raw=plan.model_dump())
    return ProviderResult(success=True, summary=plan.summary, raw=plan.model_dump())
