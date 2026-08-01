"""Git operations: never auto-merge/push, always a dedicated branch."""

from __future__ import annotations

import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

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


def _unused_branch_name(repo: git.Repo, base_name: str) -> str:
    """`base_name`, or the first `-N` variant not already taken."""
    existing = {head.name for head in repo.heads}
    if base_name not in existing:
        return base_name
    suffix = 2
    while f"{base_name}-{suffix}" in existing:
        suffix += 1
    return f"{base_name}-{suffix}"


def create_branch(repo: git.Repo, request: str) -> str:
    branch_name = _unused_branch_name(repo, f"engine/{_slugify(request)}")
    new_branch = repo.create_head(branch_name)
    new_branch.checkout()
    return branch_name


def commit_all(repo: git.Repo, summary: str) -> list[str]:
    """Detects any change made by the provider (CLI or API) and commits it.

    The provider has already written to disk by this point (providers.base.Provider
    contract): we don't need to know the file list in advance, `git add -A`
    picks up whatever changed, regardless of which provider was used.

    Commits via `repo.git.commit(...)` (shells out to the real `git` binary),
    not GitPython's `repo.index.commit(...)`: the latter resolves
    COMMIT_EDITMSG to a path shared across every worktree of a repo, not
    worktree-local, so concurrent commits from different worktrees (Phase 3)
    race on that one file. The real `git` binary handles this correctly.
    """
    repo.git.add(A=True)
    changed = [d.a_path or d.b_path for d in repo.index.diff("HEAD")]
    if changed:
        repo.git.commit("-m", f"engine: {summary}")
    return changed


def ignored_writes(repo: git.Repo) -> list[str]:
    """Paths present that `.gitignore` hides from git — and therefore from
    `commit_all`, `contracts.violations()`, and the reviewer's diff, every one
    of which derives from the tracked-file view (see #2).

    A task worktree starts with none of these: `git worktree add` only
    checks out tracked files, so anything an ignored-path scan finds there
    afterward was written by whatever ran inside it, unconditionally — no
    baseline noise to filter first. Applies to every role, including ones
    with no declared artifact contract (backend/frontend/tests): this isn't
    about *where within its scope* a role wrote, it's about a write git will
    never see at all.

    Doesn't see `.git/` itself — git never reports on its own metadata
    directory regardless of ignore rules. That's a different risk, covered
    by `disable_hooks` below rather than by anything `git status` can show.
    """
    output = repo.git.status("--porcelain", "--ignored=matching")
    return [line[3:] for line in output.splitlines() if line.startswith("!! ")]


@contextmanager
def disable_hooks(repo: git.Repo):
    """Points `core.hooksPath` at an empty, throwaway directory for the
    duration of the block, restoring whatever was configured before on exit.

    Hooks are not per-worktree: every worktree of a repo shares the main
    repo's `.git/hooks` unless `core.hooksPath` says otherwise (verified —
    a hook written from inside a task worktree survives that worktree's
    removal and fires on the very next git operation anywhere in the repo,
    e.g. the next stage's own commit or merge). That reach is exactly what
    `ignored_writes` above cannot see (`.git/` is invisible to `git status`)
    and what worktree removal cannot undo (the hook was never inside the
    worktree's own directory to begin with). Neutralizing where git looks
    closes it regardless of whether a write there is ever detected —
    detection after the fact would already be too late, since a planted
    hook can fire on this same run's remaining git operations.

    Scoped to the whole write-capable window of a run (DAG dispatch through
    the correction loop), not per-stage: the hooks path is shared config,
    concurrent per-stage toggling would race for no benefit.
    """
    # GitPython's get_value(default=...) only returns the default on a
    # *type-conversion* failure, not a missing option — passing None as the
    # default does not suppress the NoOptionError a never-configured
    # core.hooksPath actually raises, so the miss has to be caught here.
    try:
        previous = repo.config_reader().get_value("core", "hooksPath")
    except Exception:
        previous = None
    neutral_dir = Path(tempfile.mkdtemp(prefix="engine-hooks-disabled-"))
    with repo.config_writer() as writer:
        writer.set_value("core", "hooksPath", str(neutral_dir))
    try:
        yield
    finally:
        with repo.config_writer() as writer:
            if previous is None:
                writer.remove_option("core", "hooksPath")
            else:
                writer.set_value("core", "hooksPath", previous)
        shutil.rmtree(neutral_dir, ignore_errors=True)


def diff_since(repo: git.Repo, base_sha: str) -> str:
    """Diff of this run's own commit(s), not the (now-empty) working tree."""
    return repo.git.diff(base_sha, "HEAD")


def format_changed_files(files: list[str]) -> str:
    """Formats a list of changed file paths into a readable one-line summary."""
    if not files:
        return "no files changed"
    count = f"{len(files)} file" if len(files) == 1 else f"{len(files)} files"
    return f"{count} changed: {', '.join(files)}"


def prune_worktrees(repo: git.Repo) -> None:
    """Cleans git's own worktree bookkeeping (e.g. after a crashed previous
    run left a directory that no longer exists). Called once at the start of
    a run, before anything else touches worktrees."""
    repo.git.worktree("prune")


def create_worktree(repo: git.Repo, base_branch: str, task_id: str) -> tuple[Path, str]:
    """Isolated checkout for one task, branched from the current tip of
    base_branch — so it sees everything merged from earlier stages so far.

    The branch lives in a separate `engine-task/` namespace, not nested
    under `engine/<slug>`: git refs can't have one branch be both a leaf and
    a directory prefix of another (engine/<slug>/<task_id> would collide
    with engine/<slug> itself).

    The name is uniquified for the same reason `create_branch` does it.
    Without that, a stage that failed in an earlier run left its branch
    behind — deliberately, so its partial work stays inspectable (see
    remove_worktree) — and the next run of the same request died here, before
    reaching its provider, with nothing recorded to explain why.
    """
    task_branch = _unused_branch_name(
        repo, base_branch.replace("engine/", "engine-task/", 1) + f"-{task_id}"
    )
    worktree_path = Path(tempfile.mkdtemp(prefix=f"engine-{task_id}-"))
    worktree_path.rmdir()  # `git worktree add` needs to create this path itself
    repo.git.worktree("add", str(worktree_path), "-b", task_branch, base_branch)
    return worktree_path, task_branch


def merge_worktree(repo: git.Repo, task_branch: str) -> bool:
    """--no-ff merge of task_branch into the currently checked-out branch.

    Returns False (after a clean `git merge --abort`) on conflict — never
    attempts automatic resolution.
    """
    try:
        repo.git.merge(task_branch, "--no-ff", "-m", f"engine: merge {task_branch}")
        return True
    except git.GitCommandError:
        repo.git.merge("--abort")
        return False


def remove_worktree(repo: git.Repo, worktree_path: Path) -> None:
    """Removes the worktree's directory. The branch/commits it made stay
    reachable even after the directory is gone, for later inspection, as
    long as the caller doesn't also delete the branch."""
    repo.git.worktree("remove", str(worktree_path), "--force")
