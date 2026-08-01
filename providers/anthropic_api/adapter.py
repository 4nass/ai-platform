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
from providers.base import AgentTask, ProviderResult, TokenUsage, load_role_prompt

MODELS_CONFIG_PATH = Path("config/models.yaml")
READS_FILES = False  # no disk access on the way in: it only sees its prompt

TOKEN_BUDGETS = {
    "architect": 12000,
    "backend": 10000,
    "frontend": 10000,
    "reviewer": 14000,
    "security": 14000,
    "tests": 10000,
    "documentation": 5000,
}
"""Output-length cap per role. Adapter-internal plumbing, not part of the
platform's calibrated policy surface (config/platform.yaml, config/presets/):
this provider is never selected by a default profile, so it stays a plain
constant here rather than another file to keep in sync."""
DEFAULT_TOKEN_BUDGET = 10000


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


def _parse_usage(response: object, model_id: str) -> TokenUsage:
    """Maps the SDK's `Usage` into the shared shape.

    No cost: the Messages API doesn't return one, and deriving it here would
    mean hardcoding a price table that goes stale without anyone noticing.
    Leave it None and price at query time, where the rates live in one place.
    """
    usage = getattr(response, "usage", None)

    def field(name: str) -> int:
        value = getattr(usage, name, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return TokenUsage(
        model=model_id,
        input_tokens=field("input_tokens"),
        output_tokens=field("output_tokens"),
        cache_read_tokens=field("cache_read_input_tokens"),
        cache_creation_tokens=field("cache_creation_input_tokens"),
    )


def _model_id(models_config: dict) -> str:
    model_id = (models_config.get("models") or {}).get("claude", {}).get("model")
    if not model_id:
        raise ConfigError(
            f"{MODELS_CONFIG_PATH} is missing models.claude.model — cannot resolve which "
            "Anthropic model to call."
        )
    return model_id


def run(task: AgentTask) -> ProviderResult:
    models_config = _load_yaml(task.engine_root, MODELS_CONFIG_PATH)

    model_id = _model_id(models_config)
    max_tokens = TOKEN_BUDGETS.get(task.agent, DEFAULT_TOKEN_BUDGET)
    system_prompt = load_role_prompt(task.engine_root, task.agent)

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
    # Tokens were spent the moment the call returned — attach usage to the
    # failure paths too, not just the happy one.
    usage = _parse_usage(response, model_id)

    plan = response.parsed_output
    if plan is None:
        return ProviderResult(
            success=False,
            summary="The Anthropic API returned a response that didn't match the expected structured output.",
            usage=usage,
        )
    try:
        _write_files(task.repo_root, plan.files)
    except ValueError as exc:
        return ProviderResult(success=False, summary=str(exc), raw=plan.model_dump(), usage=usage)
    return ProviderResult(success=True, summary=plan.summary, raw=plan.model_dump(), usage=usage)
