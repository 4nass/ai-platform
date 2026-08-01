"""Builds the correction task description fed back to the `corrector` role
after a test or review failure. Mirrors review.py/decomposer.py's shape: a
small, focused module around one prompt-building function, nothing more.
"""

from __future__ import annotations

from core import untrusted


def build_description(request: str, *, test_output: str = "", review_summary: str = "") -> str:
    """Both inputs are untrusted, for different reasons (issue #5): the
    reviewer's findings are model output, and the test output is whatever
    agent-written tests printed to stdout — which an agent controls
    completely, and which reaches the corrector as text either way.
    """
    parts = [f'The changes made for this request are not passing verification: "{request}".']
    if test_output:
        parts.append(
            "Test failure output:\n"
            + untrusted.wrap(test_output, source="the test run", kind="output")
        )
    if review_summary:
        parts.append(
            "Reviewer findings:\n"
            + untrusted.wrap(review_summary, source="the reviewer agent", kind="findings")
        )
    parts.append(untrusted.DATA_NOT_INSTRUCTIONS)
    parts.append(
        "Make the minimal change needed to fix the above. Do not restructure "
        "unrelated code or address anything not implicated by the failure."
    )
    return "\n\n".join(parts)
