"""Builds the task-decomposition prompt and parses its selected task list.

Mirrors core.orchestrator.review's shape: a small, focused module around one
parseable output line, nothing more. The decomposer only ever narrows an
already-validated workflow (core.orchestrator.planner.prune) — it never
invents task ids or dependency edges itself.
"""

from __future__ import annotations

import re

_TASKS_RE = re.compile(r"TASKS:\s*(.+)")


def build_description(request: str, known_ids: list[str]) -> str:
    ids = ", ".join(known_ids)
    return (
        f'Decide which task types are needed for this request: "{request}".\n\n'
        f"Available task types: {ids}.\n\n"
        "End your response with exactly one line: 'TASKS: ' followed by a comma-separated "
        "subset of the task types above."
    )


def parse_tasks(text: str, known_ids: list[str]) -> list[str] | None:
    """Extracts the TASKS: line and keeps only ids that are actually in
    known_ids — a single hallucinated id shouldn't void an otherwise-usable
    decomposition. Returns None (the caller falls back to the full plan) if
    the line is missing, or if nothing valid survives the filter.
    """
    match = _TASKS_RE.search(text)
    if not match:
        return None

    known = set(known_ids)
    selected = [item.strip() for item in match.group(1).split(",")]
    valid = [item for item in selected if item in known]
    return valid or None
