"""Builds the correction task description fed back to the `corrector` role
after a test or review failure. Mirrors review.py/decomposer.py's shape: a
small, focused module around one prompt-building function, nothing more.
"""

from __future__ import annotations


def build_description(request: str, *, test_output: str = "", review_summary: str = "") -> str:
    parts = [f'The changes made for this request are not passing verification: "{request}".']
    if test_output:
        parts.append(f"Test failure output:\n```\n{test_output}\n```")
    if review_summary:
        parts.append(f"Reviewer findings:\n{review_summary}")
    parts.append(
        "Make the minimal change needed to fix the above. Do not restructure "
        "unrelated code or address anything not implicated by the failure."
    )
    return "\n\n".join(parts)
