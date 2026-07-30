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


def _write_files(repo_root: Path, files: list[FileChange]) -> list[str]:
    written = []
    for file_change in files:
        target = repo_root / file_change.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file_change.content, encoding="utf-8")
        written.append(file_change.path)
    return written


def run(task: AgentTask) -> ProviderResult:
    models_config = _load_yaml(task.repo_root, MODELS_CONFIG_PATH)
    token_budget = _load_yaml(task.repo_root, TOKEN_BUDGET_CONFIG_PATH)

    model_id = models_config["models"]["claude"]["model"]
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
    _write_files(task.repo_root, plan.files)
    return ProviderResult(success=True, summary=plan.summary, raw=plan.model_dump())
