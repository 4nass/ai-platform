"""Logical coupling between files, derived from git history.

Files that repeatedly change together in the same commit are related even
when nothing in the code links them (config alongside the code that reads
it, a module and its test file, ...).
"""

from __future__ import annotations

from itertools import combinations
from pathlib import Path
from typing import NamedTuple

import git

MAX_COMMITS = 500


class CoChange(NamedTuple):
    count: int
    strength: float
    """Sum of 1/len(commit_files) across contributing commits. A commit that
    touches many files at once (e.g. a repo-wide formatting pass) creates
    weak, largely accidental pairwise signal — this dilutes its contribution
    relative to a small, focused commit, unlike the raw count."""


def co_change_counts(repo_root: Path, max_commits: int = MAX_COMMITS) -> dict[tuple[str, str], CoChange]:
    repo = git.Repo(repo_root)
    counts: dict[tuple[str, str], int] = {}
    strengths: dict[tuple[str, str], float] = {}

    for commit in repo.iter_commits(max_count=max_commits):
        files = sorted(commit.stats.files.keys())
        if len(files) < 2:
            continue
        contribution = 1.0 / len(files)
        for a, b in combinations(files, 2):
            key = (a, b)
            counts[key] = counts.get(key, 0) + 1
            strengths[key] = strengths.get(key, 0.0) + contribution

    return {key: CoChange(count=count, strength=strengths[key]) for key, count in counts.items()}
