"""Per-role artifact contracts: which files a role is allowed to touch.

Enforced here, not just documented in prompts/<role>.md, because the CLI
tool restriction (ROLE_ALLOWED_TOOLS in providers/claude_code/adapter.py)
can only block Edit/Write entirely (used for reviewer/security) — it can't
express "may write, but only to these paths", which is what architect and
documentation need. This is the post-hoc backstop for that gap.

backend/frontend/tests are deliberately absent: their prompts only say
"only what's strictly necessary for the request" (backend/frontend) or have
a content-dependent escape hatch ("...unless a blocking bug requires it,
explain why" for tests) — neither is an enumerable path pattern a
mechanical check could enforce without false positives.
"""

from __future__ import annotations

from fnmatch import fnmatch

ROLE_ARTIFACT_PATTERNS: dict[str, list[str]] = {
    "architect": ["memory/architecture.md", "memory/adr/*.md"],
    "documentation": ["README.md", "memory/*.md", "memory/adr/*.md"],
    "security": [],  # never modifies any file -- also enforced via ROLE_ALLOWED_TOOLS (defense in depth)
}


def violations(agent: str, files_changed: list[str]) -> list[str]:
    """Files changed by `agent` that fall outside its declared contract.

    Empty result means compliant — including every role with no declared
    contract at all (backend/frontend/tests are intentionally unrestricted,
    see the module docstring).
    """
    if agent not in ROLE_ARTIFACT_PATTERNS:
        return []
    patterns = ROLE_ARTIFACT_PATTERNS[agent]
    return [path for path in files_changed if not any(fnmatch(path, pattern) for pattern in patterns)]
