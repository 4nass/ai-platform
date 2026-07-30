"""Builds the review task description and parses its PASS/FAIL verdict."""

from __future__ import annotations

import re

_VERDICT_RE = re.compile(r"VERDICT:\s*(PASS|FAIL)", re.IGNORECASE)


def build_description(request: str, diff: str) -> str:
    if not diff.strip():
        return (
            f'The task "{request}" produced no file changes. There is nothing to '
            "review. End your response with the line 'VERDICT: PASS' — no diff, no issue."
        )
    return (
        f'Review the changes made for this request: "{request}".\n\n'
        f"Diff:\n```diff\n{diff}\n```\n\n"
        "End your response with exactly one line: 'VERDICT: PASS' if there are no "
        "blocking issues, or 'VERDICT: FAIL' if there are."
    )


def parse_verdict(text: str) -> bool | None:
    """Returns True/False for a matched verdict, or None if the agent didn't
    follow the expected format — treated as a failed gate, not silently ignored."""
    match = _VERDICT_RE.search(text)
    if not match:
        return None
    return match.group(1).upper() == "PASS"
