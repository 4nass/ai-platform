"""Git operations: never auto-merge/push, always a dedicated branch."""

from __future__ import annotations

import re

import git


def ensure_clean_worktree(repo: git.Repo) -> None:
    if repo.is_dirty(untracked_files=True):
        raise RuntimeError(
            "The git working tree isn't clean: commit or stash your changes "
            "before running the prototype."
        )


def current_commit(repo: git.Repo) -> str:
    """Snapshot of HEAD before branching, used later to diff this run's own changes."""
    return repo.head.commit.hexsha


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40] or "task"


def create_branch(repo: git.Repo, request: str) -> str:
    base_name = f"hermes/{_slugify(request)}"
    branch_name = base_name
    suffix = 2
    existing = {head.name for head in repo.heads}
    while branch_name in existing:
        branch_name = f"{base_name}-{suffix}"
        suffix += 1
    new_branch = repo.create_head(branch_name)
    new_branch.checkout()
    return branch_name


def commit_all(repo: git.Repo, summary: str) -> list[str]:
    """Detects any change made by the provider (CLI or API) and commits it.

    The provider has already written to disk by this point (providers.base.Provider
    contract): we don't need to know the file list in advance, `git add -A`
    picks up whatever changed, regardless of which provider was used.
    """
    repo.git.add(A=True)
    changed = [d.a_path or d.b_path for d in repo.index.diff("HEAD")]
    if changed:
        repo.index.commit(f"hermes: {summary}")
    return changed


def diff_since(repo: git.Repo, base_sha: str) -> str:
    """Diff of this run's own commit(s), not the (now-empty) working tree."""
    return repo.git.diff(base_sha, "HEAD")


def format_changed_files(files: list[str]) -> str:
    """Formats a list of changed file paths into a readable one-line summary."""
    if not files:
        return "no files changed"
    count = f"{len(files)} file" if len(files) == 1 else f"{len(files)} files"
    return f"{count} changed: {', '.join(files)}"
