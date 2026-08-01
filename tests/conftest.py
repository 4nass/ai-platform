"""Shared pytest fixtures."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

WORKTREE_GLOB = "engine-*"
"""Matches the prefix core.orchestrator.git_ops.create_worktree passes to
`tempfile.mkdtemp`."""


@pytest.fixture(autouse=True)
def _reclaim_leaked_task_worktrees():
    """Removes task worktree directories a test left behind in the system temp
    dir.

    `git_ops.create_worktree` uses `tempfile.mkdtemp()` deliberately — a task
    worktree must live outside the repo being modified — which also puts it
    outside anything pytest's `tmp_path` cleanup can reach. Measured before
    this fixture existed: one full run of the suite left 5 directories behind,
    and 354 had accumulated across a working session (issue #1).

    Autouse and diff-based rather than opt-in: any test that reaches
    `supervisor.run` or `create_worktree` can leak, including ones added
    later that won't think to ask for cleanup. Only directories that appeared
    *during* the test are removed, so a concurrently-running real engine
    process keeps its own.
    """
    temp_root = Path(tempfile.gettempdir())
    before = set(temp_root.glob(WORKTREE_GLOB))

    yield

    for path in set(temp_root.glob(WORKTREE_GLOB)) - before:
        shutil.rmtree(path, ignore_errors=True)
