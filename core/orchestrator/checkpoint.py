"""What a run has already landed, written where a crash cannot take it with it.

A killed worker was reconciled to `interrupted` and stopped there: its branch
and integration worktree survived, so the work was *inspectable*, but nothing
recorded which stages had actually completed, so nothing could pick the run
back up. Resuming meant re-running the whole DAG — every provider call paid
for twice — or reading the branch's commit log by hand. This is the missing
piece: the per-stage record that makes `ai-platform resume` possible.

**Only the expensive, already-merged part is recorded.** Verification, review
and correction are re-run on resume rather than restored. They are one
provider call each against a tree that may have changed, so redoing them is
both cheaper than the DAG and *more* correct than trusting a stale verdict. A
merged stage is the opposite on both counts.

**Written after the merge, never before.** The checkpoint can therefore only
ever under-claim: a crash between a stage's merge and this file's update makes
the resumed run re-do that one stage, which costs a provider call. The reverse
— claiming a stage that never landed — would silently drop work from the
branch and report success, so the ordering is the whole safety argument.

**Stored in the worktree's own git directory** (`.git/worktrees/<name>/`), not
in the worktree: `git_ops.commit_all` runs `git add -A`, which would sweep a
file in the tree onto the branch under review. The git directory is invisible
to `git status` by construction, is per-worktree rather than shared, and is
removed along with the worktree when a run succeeds — so a checkpoint can
never outlive the run it describes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import git

FILENAME = "ai-platform-checkpoint.json"

VERSION = 1
"""Bumped when the shape below changes incompatibly. An unreadable checkpoint
is not an error — it means "this run cannot be resumed", which the caller
turns into a fresh run rather than a crash."""


@dataclass(frozen=True)
class StageRecord:
    """One DAG stage whose work is merged into the run's branch.

    `summary` and `files_changed` are kept because they are what downstream
    stages are told about their upstreams (`scheduler.build_stage_description`).
    A resumed run that skipped a stage but could not describe it would hand the
    next agent a prompt missing exactly the context the skipped stage produced.
    """

    id: str
    agent: str
    summary: str = ""
    files_changed: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Checkpoint:
    base_sha: str
    """The commit the run branched from. Restored rather than re-derived: the
    target's HEAD may have moved since, and diffing the review against a
    different base would describe changes this run never made."""

    branch: str
    request: str
    complexity: str
    """The decomposer's classification, kept so a resume does not have to call
    it again — a provider call whose answer could differ from the one the
    completed stages were actually routed under."""

    task_ids: list[str] = field(default_factory=list)
    """The workflow after pruning. Same reason as `complexity`: re-deciding the
    task set on resume could contradict what is already merged."""

    stages: list[StageRecord] = field(default_factory=list)
    version: int = VERSION

    @property
    def completed_ids(self) -> set[str]:
        return {stage.id for stage in self.stages}


def path_for(integration_root: Path) -> Path:
    """Where this worktree's checkpoint lives.

    Raises whatever GitPython raises for a path that is not a worktree —
    callers that are guessing should use `load`, which answers None.
    """
    return Path(git.Repo(integration_root).git_dir) / FILENAME


def save(integration_root: Path, state: Checkpoint) -> None:
    """Records progress. Best-effort by contract: a checkpoint that cannot be
    written costs a resumed run some repeated work, while raising here would
    cost the run itself — and this is called from the middle of a DAG walk
    that has real, merged work behind it."""
    try:
        path_for(integration_root).write_text(
            json.dumps(asdict(state), indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def record_stage(
    integration_root: Path, state: Checkpoint, stage: StageRecord
) -> Checkpoint:
    """Appends a merged stage and persists it, returning the new checkpoint.

    Returns rather than mutates because `Checkpoint` is frozen: the caller
    holds one snapshot per run and rebinds it, so there is no shared object
    for a still-running stage to observe half-updated.
    """
    updated = Checkpoint(
        base_sha=state.base_sha,
        branch=state.branch,
        request=state.request,
        complexity=state.complexity,
        task_ids=state.task_ids,
        stages=[*state.stages, stage],
    )
    save(integration_root, updated)
    return updated


def load(integration_root: Path) -> Checkpoint | None:
    """The checkpoint for a worktree, or None if there is nothing to resume.

    None covers every way this can legitimately fail — the directory is gone,
    it was never a worktree, no run ever wrote there, the file is truncated
    from a crash mid-write, or it comes from an incompatible version. All of
    them mean the same thing to a caller ("start over"), and none of them is
    worth failing a command over.
    """
    try:
        raw = json.loads(path_for(Path(integration_root)).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict) or raw.get("version") != VERSION:
        return None
    try:
        stages = [StageRecord(**stage) for stage in raw.pop("stages", [])]
        return Checkpoint(**{**raw, "stages": stages})
    except TypeError:
        return None
