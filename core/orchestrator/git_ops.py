"""Opérations git : jamais de merge/push automatique, toujours une branche dédiée."""

from __future__ import annotations

import re

import git


def ensure_clean_worktree(repo: git.Repo) -> None:
    if repo.is_dirty(untracked_files=True):
        raise RuntimeError(
            "Le working tree git n'est pas propre : commit ou stash tes changements "
            "avant de lancer le prototype."
        )


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
    """Détecte tout changement fait par le provider (CLI ou API) et le commit.

    Le provider a déjà écrit sur disque à ce stade (contrat providers.base.Provider) :
    on n'a pas besoin de connaître la liste des fichiers à l'avance, `git add -A`
    ramasse tout ce qui a changé, peu importe le provider utilisé.
    """
    repo.git.add(A=True)
    changed = [d.a_path or d.b_path for d in repo.index.diff("HEAD")]
    if changed:
        repo.index.commit(f"hermes: {summary}")
    return changed
