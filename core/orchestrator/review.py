"""Builds the review task description and parses its PASS/FAIL verdict."""

from __future__ import annotations

import re

# Anchored to the start of a line (and tolerant of markdown emphasis), then
# the LAST such line wins. Both matter: the reviewer is told to *end* its
# response with this line, but it also quotes the diff it reviewed — and a
# diff can legitimately contain the string "VERDICT: PASS" (this repo's own
# tests/test_supervisor.py has several). A plain `search()` for it anywhere
# took the first, quoted occurrence and could turn a real FAIL into a PASS,
# silently disabling the review gate.
_VERDICT_RE = re.compile(r"^\**VERDICT:\**\s*(PASS|FAIL)", re.IGNORECASE | re.MULTILINE)


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
    follow the expected format — treated as a failed gate, not silently ignored.

    Deliberately strict (fails closed): a verdict that isn't on its own line
    yields None, which the supervisor treats as a failed gate. Being too
    lenient here is what makes the gate bypassable; being too strict only
    costs a re-run.
    """
    verdicts = _VERDICT_RE.findall(text)
    if not verdicts:
        return None
    return verdicts[-1].upper() == "PASS"
