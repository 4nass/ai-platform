"""Git operations: never auto-merge/push, always a dedicated branch."""

from __future__ import annotations

import fcntl
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

import git

from core.orchestrator import target_config

HOOKS_DISABLED_PREFIX = "engine-hooks-disabled-"
SAVED_HOOKS_SECTION = "ai-platform"
SAVED_HOOKS_OPTION = "savedHooksPath"
UNSET_SENTINEL = "<unset>"
"""Distinguishes "the user had no `core.hooksPath`" from "the user had one" in
a config file that has no way to store None. Restoring the wrong one of those
either strands a setting or invents one."""

ENGINE_INDEX_DIR = ".ai-platform"
"""The engine's own derived artifacts under a target repo (vector index,
graph cache). Excluded from `local_modifications` for the same reason
`graph.builder._source_tree_is_dirty` excludes it: it's something the engine
wrote, not something the user is in the middle of."""


def local_modifications(repo: git.Repo) -> list[str]:
    """Paths in this checkout that differ from what HEAD records — tracked
    modifications and untracked files alike.

    Not a precondition for a run, and not a warning either. A run works only
    on `base_sha`: the integration worktree is checked out from it, and every
    prompt is built from that worktree (see `supervisor.run`). So these paths
    are simply *not part of the run*, and the count is reported to say so.

    Returning the list rather than a bool because the number is the message.
    Telling someone their tree is dirty is noise — they know. Telling them
    four specific edits are outside what the agents will see is information.
    """
    output = repo.git.status(
        "--porcelain", "--untracked-files=all", "--", ".", f":(exclude){ENGINE_INDEX_DIR}"
    )
    # `XY <path>` per line; the two status columns are fixed-width, so the
    # path starts at 3. A rename reads `R  old -> new`, which is fine here:
    # this feeds a count and a short listing, not a path lookup.
    return [line[3:].strip() for line in output.splitlines() if line.strip()]


def current_commit(repo: git.Repo) -> str:
    """Snapshot of HEAD before branching, used later to diff this run's own changes."""
    return repo.head.commit.hexsha


def current_ref(repo: git.Repo) -> str:
    """The branch a run started from, or `HEAD` on a detached checkout.

    Recorded alongside `base_sha` rather than derived from it: a sha says
    which commit, a ref says which line of work someone thought they were on,
    and after a force-push those two stop agreeing. Detached HEAD is reported
    as such instead of guessed at — there is no branch to name.
    """
    try:
        return repo.active_branch.name
    except TypeError:
        return "HEAD"  # detached


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


def create_integration_worktree(repo: git.Repo, request: str) -> tuple[Path, str]:
    """The run's own checkout of a fresh `engine/<slug>` branch.

    Replaces checking that branch out in the caller's working tree, which
    made a run invasive in three ways at once: it switched the user's HEAD
    out from under them, left them on the engine branch afterwards, and made
    two concurrent runs impossible since both wanted the same tree. Every
    stage's worktree branches from here, and every merge/commit/diff for the
    run happens here — the target repo's own checkout is never written to.

    Branched from the repo's current HEAD, not from its working tree: a
    worktree checkout only ever contains committed state, so uncommitted
    work in the target is invisible to the run. That's what makes running
    against a dirty tree safe — and, since `supervisor.run` now also builds
    every prompt from this checkout rather than from the target's, what makes
    it *coherent*: what the agents read and what they edit are the same tree.
    """
    branch_name = _unused_branch_name(repo, f"engine/{_slugify(request)}")
    worktree_path = Path(tempfile.mkdtemp(prefix="engine-run-"))
    worktree_path.rmdir()  # `git worktree add` needs to create this path itself
    repo.git.worktree("add", str(worktree_path), "-b", branch_name, "HEAD")
    return worktree_path, branch_name


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


def classify_ignored_writes(
    repo: git.Repo, allowed_patterns: tuple[str, ...], baseline: set[str] | None = None
) -> tuple[list[str], list[str]]:
    """Splits gitignored writes into (expected ephemeral, real violations).

    A run that executes the target's test command will legitimately produce
    tool caches — `.pytest_cache/`, `__pycache__/`, `.coverage`. Found by a
    real run, where a `backend` stage was rejected for exactly that and its
    work discarded. Treating every ignored write as hostile is wrong; so is
    treating them all as fine, which is the hole issue #2 closed. The target
    declares which ones it expects (`allowed_ephemeral_writes`) and anything
    else is still a violation.

    `baseline` is the set already present before this actor ran. Taken per
    actor rather than once per run: a worktree stage starts from a clean
    checkout and has none, but the corrector runs on a worktree that earlier
    steps have already used.
    """
    baseline = baseline or set()
    expected: list[str] = []
    violations: list[str] = []
    for path in ignored_writes(repo):
        if path in baseline:
            continue
        (expected if target_config.matches_any(path, allowed_patterns) else violations).append(path)
    return expected, violations


def create_validation_worktree(repo: git.Repo, branch: str) -> Path:
    """A throwaway checkout of `branch`, for running the target's tests.

    The test command is the one actor guaranteed to litter: pytest writes
    `.pytest_cache/`, coverage writes `.coverage`, Python writes
    `__pycache__/`. Running it in a worktree that is deleted immediately
    afterwards means none of that can be mistaken for something the
    *corrector* wrote, and none of it reaches the branch being reviewed.

    Detached rather than checked out as a branch: `branch` is already checked
    out in the integration worktree, and git refuses to have one branch live
    in two worktrees at once.
    """
    path = Path(tempfile.mkdtemp(prefix="engine-verify-"))
    path.rmdir()  # `git worktree add` needs to create this path itself
    repo.git.worktree("add", "--detach", str(path), branch)
    return path


@contextmanager
def exclusive_run_lock(repo: git.Repo):
    """Serializes mutating runs against one repository.

    `disable_hooks` rewrites `core.hooksPath`, which is repository-wide
    config shared by every worktree — so two concurrent runs would race to
    restore it and one could leave the other's neutralized path behind, or
    restore a value the other still needs. Integration worktrees are
    per-run and safe; this shared config is not, which makes one mutating
    run per repo the honest boundary until that's addressed properly.

    Fails fast rather than waiting: a second run blocking silently for the
    length of the first is worse than being told why it can't start.

    Must be acquired before the run's first mutating operation, not after —
    the first version took it once the integration worktree already existed,
    so the run that got refused had already pruned, created a branch and
    checked out a worktree. Rejection has to leave nothing behind, which
    means the lock comes first and everything else happens inside it.
    """
    lock_path = Path(repo.git_dir) / "ai-platform-run.lock"
    handle = lock_path.open("w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError(
                f"Another ai-platform run is already modifying {repo.working_tree_dir}. "
                "Runs share this repository's git config (see git_ops.disable_hooks), so only "
                "one mutating run at a time is safe. Wait for it to finish, or use --repo to "
                "point at a different checkout."
            ) from None
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


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

    The previous value is written to the repo's own config before the swap,
    not just held in memory, so `restore_hooks` can undo this after a crash.
    A `finally` only runs if the process lives to run it: killing a worker
    mid-run (which is now a first-class case — see core.jobs) otherwise left
    the target pointing at the neutral directory *permanently*, silently
    disabling the user's own hooks in their repository. Measured on a real
    SIGKILL'd run before this was added.
    """
    # Repair what a previous run left behind *before* reading the current
    # value, rather than trusting reconciliation to have done it. Two reasons,
    # both measured on a real crashed-then-clean pair:
    #
    # - Reconciliation only runs on the jobs path (core.jobs.worker). A
    #   synchronous `ai-platform run` that gets killed has nothing that ever
    #   learns it died, so its leak had no repair path at all.
    # - Worse, the leak compounded. `core.hooksPath` after a crash *is* this
    #   engine's own neutral directory, so reading it as "previous" made the
    #   next run save the neutralization as the value to restore — and a clean
    #   exit then put it back and dropped the saved key, leaving the repo
    #   pointing at a directory that no longer exists with nothing left to
    #   repair it from. A recoverable leak became permanent damage, and the
    #   run that did it was working perfectly.
    restore_hooks(repo)
    previous = _configured_hooks_path(repo)
    if previous is not None and _is_engine_owned(previous):
        # Still ours even after the repair: this repo was damaged by a run
        # from before the above existed, and the value the user actually had
        # is gone. Unset is the honest answer — it is what git assumes by
        # default — and leaving the stale path in place would keep their hooks
        # disabled forever.
        shutil.rmtree(previous, ignore_errors=True)
        previous = None

    neutral_dir = Path(tempfile.mkdtemp(prefix=HOOKS_DISABLED_PREFIX))
    with repo.config_writer() as writer:
        writer.set_value(SAVED_HOOKS_SECTION, SAVED_HOOKS_OPTION, previous or UNSET_SENTINEL)
        writer.set_value("core", "hooksPath", str(neutral_dir))
    try:
        yield
    finally:
        restore_hooks(repo)
        shutil.rmtree(neutral_dir, ignore_errors=True)


def _configured_hooks_path(repo: git.Repo) -> str | None:
    """`core.hooksPath`, or None when it was never set.

    GitPython's `get_value(default=...)` only returns the default on a *type
    conversion* failure, not a missing option — passing None as the default
    does not suppress the NoOptionError a never-configured `core.hooksPath`
    actually raises, so the miss has to be caught here.
    """
    try:
        return str(repo.config_reader().get_value("core", "hooksPath"))
    except Exception:
        return None


def _is_engine_owned(hooks_path: str) -> bool:
    """Whether a `core.hooksPath` is one of this engine's neutral directories.

    Recognised by name rather than by remembering which one this process
    created: the process that created it is gone in every case that matters.
    """
    return Path(hooks_path).name.startswith(HOOKS_DISABLED_PREFIX)


def restore_hooks(repo: git.Repo) -> bool:
    """Undoes `disable_hooks`, whether or not the run that called it survived.

    Returns True if it changed anything. Idempotent and safe to call on a repo
    no run ever touched: with nothing saved, there is nothing to put back.

    Only ever reverses the engine's *own* neutralization. If `core.hooksPath`
    now points somewhere that isn't an engine directory, someone set it
    deliberately after the crash and it is not this function's to overwrite —
    the saved value is dropped instead, so a later call can't resurrect a
    stale path over a deliberate one.
    """
    saved = _saved_hooks_path(repo)
    if saved is None:
        return False
    if saved != UNSET_SENTINEL and _is_engine_owned(saved):
        # A leak that a pre-fix run recorded as the value to put back. Doing
        # so would reinstate the neutralization; nothing here knows what the
        # user actually had, so unset is the only honest restore.
        saved = UNSET_SENTINEL

    current = _configured_hooks_path(repo)
    engine_owned = current is not None and _is_engine_owned(current)

    with repo.config_writer() as writer:
        if engine_owned:
            if saved == UNSET_SENTINEL:
                writer.remove_option("core", "hooksPath")
            else:
                writer.set_value("core", "hooksPath", saved)
        writer.remove_option(SAVED_HOOKS_SECTION, SAVED_HOOKS_OPTION)
    return engine_owned


def _saved_hooks_path(repo: git.Repo) -> str | None:
    try:
        return str(repo.config_reader().get_value(SAVED_HOOKS_SECTION, SAVED_HOOKS_OPTION))
    except Exception:
        return None


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


def stage_worktrees(repo: git.Repo, base_branch: str) -> list[tuple[Path, str]]:
    """Task worktrees still registered under a run's branch namespace.

    What a killed run leaves behind: a stage that was mid-flight never got to
    commit, so its worktree is neither merged nor removed, and `worktree prune`
    will not reclaim it — the directory is still there, which is precisely why
    prune leaves it alone. A resumed run re-runs that stage in a fresh
    worktree, so this exists to *name* the old one rather than to delete it:
    the checkout may hold uncommitted agent work, and guessing that it doesn't
    is not this function's call to make.
    """
    prefix = base_branch.replace("engine/", "engine-task/", 1) + "-"
    found: list[tuple[Path, str]] = []
    path: Path | None = None
    for line in repo.git.worktree("list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :])
        elif line.startswith("branch ") and path is not None:
            name = line[len("branch refs/heads/") :]
            if name.startswith(prefix):
                found.append((path, name))
    return found


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
