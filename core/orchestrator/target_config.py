"""The target repo's own declaration of how a run may treat it.

`.ai-platform.yml` says which command validates a change, whether that
command is sandboxed, and which throwaway paths a run is allowed to leave
behind. All of that is *security policy for the run*, which is why it is
read once, from the run's **base commit**, and then never re-read.

The reason is concrete, not theoretical. Roles without an artifact contract
(backend, frontend, tests, corrector — see core.orchestrator.contracts) may
write any tracked file, `.ai-platform.yml` included. When the final test run
re-read that file off the integration worktree, a stage could commit

    test_sandbox: false
    test_command: [...anything...]

and the engine would honour it: the sandbox from issue #4 was disabled by
the very code it was meant to contain, and the run still reported `done`.
Demonstrated end to end before this module existed.

So: a run reads the policy that was in effect when it started. A change to
`.ai-platform.yml` on the produced branch is a normal reviewable change that
takes effect for the *next* run — never for the one that wrote it. A run can
never grant itself permissions.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import PurePosixPath

import git

from core.errors import ConfigError

CONFIG_PATH = ".ai-platform.yml"
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_SANDBOX_CACHE_DIRS = ("~/.cache",)


@dataclass(frozen=True)
class TargetConfig:
    """Immutable for the lifetime of one run. Frozen so a later mutation is a
    TypeError at the point of the bug rather than a silent policy change."""

    test_command: tuple[str, ...] | None = None
    test_timeout: int = DEFAULT_TIMEOUT_SECONDS
    test_sandbox: bool = True
    sandbox_cache_dirs: tuple[str, ...] = DEFAULT_SANDBOX_CACHE_DIRS
    test_env: tuple[tuple[str, str], ...] = ()
    allowed_ephemeral_writes: tuple[str, ...] = ()
    """Gitignored paths a run may leave behind without it counting as a
    contract violation — tool caches, coverage data, compiled artifacts.

    Deliberately empty by default and declared per project. A shipped
    catch-all list would gradually reopen exactly the hole issue #2 closed:
    every pattern here is a path an agent can write that the reviewer's diff
    will never show.
    """

    @property
    def env(self) -> dict[str, str]:
        return dict(self.test_env)


def _validate_pattern(pattern: str) -> str:
    """Rejects patterns that would neuter the ignored-write check.

    The check exists to catch writes git will never show a reviewer. A
    pattern that matches the whole repo, escapes it, or covers `.git/` would
    hand that back — so those are configuration errors, not judgement calls
    left to whoever edits the file.
    """
    if not isinstance(pattern, str) or not pattern.strip():
        raise ConfigError(f"{CONFIG_PATH}: allowed_ephemeral_writes entries must be non-empty strings")

    cleaned = pattern.strip()
    if cleaned.startswith("/") or (len(cleaned) > 1 and cleaned[1] == ":"):
        raise ConfigError(
            f"{CONFIG_PATH}: allowed_ephemeral_writes must be repo-relative, got absolute path {cleaned!r}"
        )
    parts = PurePosixPath(cleaned).parts
    if ".." in parts:
        raise ConfigError(
            f"{CONFIG_PATH}: allowed_ephemeral_writes must stay inside the repo, got {cleaned!r}"
        )
    if parts and parts[0] == ".git":
        raise ConfigError(
            f"{CONFIG_PATH}: allowed_ephemeral_writes may not cover .git/, got {cleaned!r} — a write "
            "there is the persistence vector issue #2 is about, not an ephemeral artifact"
        )
    if not cleaned.replace("*", "").replace("/", "").replace("?", ""):
        raise ConfigError(
            f"{CONFIG_PATH}: allowed_ephemeral_writes pattern {cleaned!r} matches the whole repo, "
            "which would disable the ignored-write check entirely"
        )
    return cleaned


def _variants(pattern: str) -> set[str]:
    """The equivalent spellings one declared pattern should also cover.

    Two rewrites, both driven by what `git status --ignored=matching`
    actually emits — it reports a mixture of individual files and collapsed
    directory entries (`.pytest_cache/v/`), so a pattern written for one
    form has to match the other:

    - a leading `**/` is optional, so `**/__pycache__/**` covers a
      root-level `__pycache__/` without the project writing the rule twice;
    - a trailing `/**` or `/*` also covers the directory entry itself, so
      `.pytest_cache/**` covers the bare `.pytest_cache/` line.
    """
    bare = pattern.rstrip("/")
    out = {bare}
    if bare.startswith("**/"):
        out.add(bare[3:])
    for variant in list(out):
        for suffix in ("/**", "/*"):
            if variant.endswith(suffix):
                out.add(variant[: -len(suffix)])
    return {v for v in out if v}


def matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    """Whether a repo-relative path is covered by one of the patterns.

    `fnmatch` rather than `PurePath.match`: `*` is meant to cross directory
    separators here (`.pytest_cache/**` should cover `.pytest_cache/v/x`),
    which `match` won't do.
    """
    candidate = path.rstrip("/")
    for pattern in patterns:
        for variant in _variants(pattern):
            if fnmatch(candidate, variant) or fnmatch(candidate, f"{variant}/*"):
                return True
    return False


def _parse(data: dict) -> TargetConfig:
    command = data.get("test_command")
    if isinstance(command, str):
        command = tuple(shlex.split(command))
    elif isinstance(command, list) and all(isinstance(part, str) for part in command):
        command = tuple(command)
    else:
        command = None

    timeout = data.get("test_timeout", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        timeout = DEFAULT_TIMEOUT_SECONDS

    sandbox = data.get("test_sandbox", True)
    if not isinstance(sandbox, bool):
        sandbox = True

    cache_dirs = data.get("sandbox_cache_dirs", list(DEFAULT_SANDBOX_CACHE_DIRS))
    if not isinstance(cache_dirs, list) or not all(isinstance(d, str) for d in cache_dirs):
        cache_dirs = list(DEFAULT_SANDBOX_CACHE_DIRS)

    env = data.get("test_env", {})
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        env = {}

    ephemeral = data.get("allowed_ephemeral_writes", [])
    if ephemeral is None:
        ephemeral = []
    if not isinstance(ephemeral, list):
        raise ConfigError(f"{CONFIG_PATH}: allowed_ephemeral_writes must be a list of patterns")

    return TargetConfig(
        test_command=command,
        test_timeout=int(timeout),
        test_sandbox=sandbox,
        sandbox_cache_dirs=tuple(cache_dirs),
        test_env=tuple(sorted(env.items())),
        allowed_ephemeral_writes=tuple(_validate_pattern(p) for p in ephemeral),
    )


def load_at_commit(repo: git.Repo, commit: str) -> TargetConfig:
    """The policy as committed, not as it currently sits on disk.

    Reading from the commit rather than the working tree matters twice over:
    it is what makes the policy immune to a stage rewriting it mid-run, and
    it stays correct when the target's tree is dirty (allowed since the run
    moved into its own integration worktree).
    """
    import yaml

    try:
        raw = repo.git.show(f"{commit}:{CONFIG_PATH}")
    except git.GitCommandError:
        return TargetConfig()  # not committed — same as absent
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{CONFIG_PATH} must be a mapping")
    return _parse(data)
