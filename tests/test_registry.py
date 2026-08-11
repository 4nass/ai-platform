"""Tests for core.orchestrator.registry — the repository allowlist (issue #25).

The properties here are admission properties: what an untrusted caller can
name, and what it cannot reach by naming it cleverly. Most of these are
written as escape attempts rather than as feature checks, because that is the
only way this module can fail that matters.
"""

from __future__ import annotations

from pathlib import Path

import git
import pytest

from core.orchestrator import registry
from core.orchestrator.registry import RegistryError


@pytest.fixture
def engine(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    return tmp_path


def _repo(path: Path, *, remote: str | None = None, branch: str | None = None) -> git.Repo:
    path.mkdir(parents=True, exist_ok=True)
    repo = git.Repo.init(path)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test")
        writer.set_value("user", "email", "test@example.com")
    (path / "file.txt").write_text("x", encoding="utf-8")
    repo.index.add(["file.txt"])
    repo.index.commit("initial")
    if remote:
        repo.create_remote("origin", remote)
    if branch:
        repo.create_head(branch)
    return repo


def _write(engine: Path, body: str) -> None:
    (engine / registry.CONFIG_PATH).write_text(body, encoding="utf-8")


# --- loading ---


def test_an_absent_registry_is_empty_not_an_error(engine: Path) -> None:
    """The local CLI works entirely through `--repo` and declares nothing. What
    an absent file must never do is let an id through — that is `resolve`."""
    assert registry.load(engine) == {}


def test_a_project_round_trips_with_its_policy(engine: Path, tmp_path: Path) -> None:
    _repo(tmp_path / "roots" / "mine")
    _write(
        engine,
        f"""
roots: [{tmp_path / "roots"}]
projects:
  mine:
    path: {tmp_path / "roots" / "mine"}
    base_branch: master
    allowed_actions: [inspect, modify, test]
    budget_class: generous
""",
    )

    project = registry.load(engine)["mine"]

    assert project.id == "mine"
    assert project.path == (tmp_path / "roots" / "mine").resolve()
    assert project.allowed_actions == ("inspect", "modify", "test")
    assert project.budget_class == "generous"


def test_a_project_that_declares_no_actions_is_read_only(engine: Path, tmp_path: Path) -> None:
    """An entry added in a hurry should be the least dangerous thing in the
    file, not the most."""
    _repo(tmp_path / "roots" / "mine")
    _write(engine, f"roots: [{tmp_path / 'roots'}]\nprojects:\n  mine:\n    path: {tmp_path / 'roots' / 'mine'}\n")

    project = registry.load(engine)["mine"]

    assert project.allowed_actions == ("inspect",)
    assert not project.permits("modify")


def test_an_unknown_action_in_the_registry_is_refused(engine: Path, tmp_path: Path) -> None:
    _repo(tmp_path / "roots" / "mine")
    _write(
        engine,
        f"roots: [{tmp_path / 'roots'}]\nprojects:\n  mine:\n"
        f"    path: {tmp_path / 'roots' / 'mine'}\n    allowed_actions: [inspect, rm_rf]\n",
    )

    with pytest.raises(RegistryError, match="unknown allowed_actions rm_rf"):
        registry.load(engine)


# --- containment: the allowlist's teeth ---


def test_a_project_outside_every_root_is_refused(engine: Path, tmp_path: Path) -> None:
    _repo(tmp_path / "elsewhere" / "mine")
    _write(
        engine,
        f"roots: [{tmp_path / 'roots'}]\nprojects:\n  mine:\n    path: {tmp_path / 'elsewhere' / 'mine'}\n",
    )

    with pytest.raises(RegistryError, match="outside every declared root"):
        registry.load(engine)


def test_a_symlink_cannot_escape_the_allowlist(engine: Path, tmp_path: Path) -> None:
    """The reason paths are resolved rather than normalized textually: a link
    planted anywhere along the path would otherwise read as inside."""
    (tmp_path / "roots").mkdir()
    _repo(tmp_path / "secret")
    (tmp_path / "roots" / "innocent").symlink_to(tmp_path / "secret")
    _write(
        engine,
        f"roots: [{tmp_path / 'roots'}]\nprojects:\n  mine:\n    path: {tmp_path / 'roots' / 'innocent'}\n",
    )

    with pytest.raises(RegistryError, match="outside every declared root"):
        registry.load(engine)


def test_a_dotdot_spelling_cannot_escape_the_allowlist(engine: Path, tmp_path: Path) -> None:
    _repo(tmp_path / "elsewhere" / "mine")
    (tmp_path / "roots").mkdir()
    _write(
        engine,
        f"roots: [{tmp_path / 'roots'}]\nprojects:\n  mine:\n"
        f"    path: {tmp_path / 'roots'}/../elsewhere/mine\n",
    )

    with pytest.raises(RegistryError, match="outside every declared root"):
        registry.load(engine)


def test_a_sibling_directory_sharing_a_name_prefix_is_not_inside(
    engine: Path, tmp_path: Path
) -> None:
    """`/srv/workspace-evil` starts with `/srv/workspace` as a string and is a
    sibling of it as a directory. A prefix check would admit it."""
    (tmp_path / "workspace").mkdir()
    _repo(tmp_path / "workspace-evil")
    _write(
        engine,
        f"roots: [{tmp_path / 'workspace'}]\nprojects:\n  mine:\n    path: {tmp_path / 'workspace-evil'}\n",
    )

    with pytest.raises(RegistryError, match="outside every declared root"):
        registry.load(engine)


def test_a_registry_with_projects_but_no_roots_is_refused(engine: Path, tmp_path: Path) -> None:
    """Without a boundary there is no allowlist, only a list."""
    _repo(tmp_path / "mine")
    _write(engine, f"projects:\n  mine:\n    path: {tmp_path / 'mine'}\n")

    with pytest.raises(RegistryError, match="no `roots`"):
        registry.load(engine)


def test_the_resolved_path_is_reported_when_it_differs_from_the_declared_one(
    engine: Path, tmp_path: Path
) -> None:
    """When a symlink is what escaped, the declared spelling is precisely the
    misleading half — so the error has to show both."""
    (tmp_path / "roots").mkdir()
    _repo(tmp_path / "secret")
    (tmp_path / "roots" / "innocent").symlink_to(tmp_path / "secret")
    _write(
        engine,
        f"roots: [{tmp_path / 'roots'}]\nprojects:\n  mine:\n    path: {tmp_path / 'roots' / 'innocent'}\n",
    )

    with pytest.raises(RegistryError) as caught:
        registry.load(engine)

    assert str((tmp_path / "secret").resolve()) in str(caught.value)
    assert "innocent" in str(caught.value)


# --- resolve: the admission path ---


def _allowlisted(engine: Path, tmp_path: Path, *, actions: str = "[inspect, modify, test]", **extra) -> Path:
    path = tmp_path / "roots" / "mine"
    _repo(path, remote=extra.get("remote"), branch=extra.get("branch"))
    lines = [f"roots: [{tmp_path / 'roots'}]", "projects:", "  mine:", f"    path: {path}", f"    allowed_actions: {actions}"]
    if extra.get("remote"):
        lines.append(f"    remote: {extra['remote']}")
    if extra.get("base_branch"):
        lines.append(f"    base_branch: {extra['base_branch']}")
    if extra.get("sync_policy"):
        lines.append(f"    sync_policy: {extra['sync_policy']}")
    _write(engine, "\n".join(lines) + "\n")
    return path


def test_sync_policy_defaults_to_offline_and_is_snapshotted(engine: Path, tmp_path: Path) -> None:
    _allowlisted(engine, tmp_path)

    project = registry.resolve(engine, "mine")

    assert project.sync_policy == registry.SYNC_OFFLINE
    assert project.snapshot()["project_sync_policy"] == registry.SYNC_OFFLINE


def test_remote_sync_policy_requires_remote_and_base_branch(engine: Path, tmp_path: Path) -> None:
    path = tmp_path / "roots" / "mine"
    _repo(path)
    _write(engine, f"roots: [{tmp_path / 'roots'}]\nprojects:\n  mine:\n    path: {path}\n    sync_policy: fetch\n")

    with pytest.raises(RegistryError, match="requires a remote"):
        registry.load(engine)


def test_unknown_sync_policy_is_refused(engine: Path, tmp_path: Path) -> None:
    _allowlisted(engine, tmp_path, sync_policy="never")

    with pytest.raises(RegistryError, match="unknown sync_policy"):
        registry.load(engine)


def test_resolve_returns_an_allowlisted_project(engine: Path, tmp_path: Path) -> None:
    path = _allowlisted(engine, tmp_path)

    project = registry.resolve(engine, "mine", action=registry.MODIFY)

    assert project.path == path.resolve()


def test_resolve_refuses_an_unknown_project(engine: Path, tmp_path: Path) -> None:
    _allowlisted(engine, tmp_path)

    with pytest.raises(RegistryError, match="No project 'theirs'"):
        registry.resolve(engine, "theirs")


def test_an_unknown_project_error_does_not_list_what_exists(
    engine: Path, tmp_path: Path
) -> None:
    """A registry listing is inventory of the owner's machine, and probing for
    valid ids is the first thing an unauthorized caller would do."""
    _allowlisted(engine, tmp_path)

    with pytest.raises(RegistryError) as caught:
        registry.resolve(engine, "theirs")

    assert "mine" not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_resolve_refuses_an_action_the_project_does_not_permit(
    engine: Path, tmp_path: Path
) -> None:
    _allowlisted(engine, tmp_path, actions="[inspect]")

    with pytest.raises(RegistryError, match="does not permit 'modify'"):
        registry.resolve(engine, "mine", action=registry.MODIFY)


def test_resolve_refuses_an_action_the_engine_does_not_define(
    engine: Path, tmp_path: Path
) -> None:
    _allowlisted(engine, tmp_path)

    with pytest.raises(RegistryError, match="Unknown action 'deploy'"):
        registry.resolve(engine, "mine", action="deploy")


def test_resolve_refuses_a_project_id_with_no_registry_at_all(engine: Path) -> None:
    with pytest.raises(RegistryError, match="No project"):
        registry.resolve(engine, "mine")


# --- verify: the path is not the identity ---


def test_a_target_that_is_not_a_git_repository_is_refused(engine: Path, tmp_path: Path) -> None:
    (tmp_path / "roots" / "mine").mkdir(parents=True)
    _write(engine, f"roots: [{tmp_path / 'roots'}]\nprojects:\n  mine:\n    path: {tmp_path / 'roots' / 'mine'}\n")

    with pytest.raises(RegistryError, match="not a git repository"):
        registry.resolve(engine, "mine")


def test_a_missing_target_is_refused(engine: Path, tmp_path: Path) -> None:
    (tmp_path / "roots").mkdir()
    _write(engine, f"roots: [{tmp_path / 'roots'}]\nprojects:\n  mine:\n    path: {tmp_path / 'roots' / 'gone'}\n")

    with pytest.raises(RegistryError, match="does not exist"):
        registry.resolve(engine, "mine")


def test_a_repository_with_the_wrong_remote_is_refused(engine: Path, tmp_path: Path) -> None:
    """A directory can be replaced or re-cloned from somewhere else. The path
    being the same does not make the repository the same."""
    _allowlisted(engine, tmp_path, remote="https://github.com/someone/else.git")
    _write(
        engine,
        f"roots: [{tmp_path / 'roots'}]\nprojects:\n  mine:\n"
        f"    path: {tmp_path / 'roots' / 'mine'}\n"
        f"    remote: https://github.com/4nass/expected.git\n",
    )

    with pytest.raises(RegistryError, match="the repository is not"):
        registry.resolve(engine, "mine")


def test_the_declared_remote_is_accepted_when_it_matches(engine: Path, tmp_path: Path) -> None:
    remote = "https://github.com/4nass/mine.git"
    _allowlisted(engine, tmp_path, remote=remote, base_branch="master")

    assert registry.resolve(engine, "mine").remote == remote


def test_a_missing_base_branch_is_refused(engine: Path, tmp_path: Path) -> None:
    _allowlisted(engine, tmp_path, base_branch="release")

    with pytest.raises(RegistryError, match="does not exist"):
        registry.resolve(engine, "mine")


def test_a_project_with_no_declared_remote_is_not_checked_against_one(
    engine: Path, tmp_path: Path
) -> None:
    """A purely local project must be expressible without inventing a remote
    for it — an unset field is unchecked, not checked against the empty
    string."""
    _allowlisted(engine, tmp_path)

    assert registry.resolve(engine, "mine").remote == ""


# --- the snapshot recorded on the run ---


def test_the_policy_snapshot_carries_what_a_run_was_judged_under(
    engine: Path, tmp_path: Path
) -> None:
    """A run is only interpretable against the policy in force when it started,
    and this file can be edited afterwards."""
    _allowlisted(engine, tmp_path, actions="[inspect, modify]")

    snapshot = registry.resolve(engine, "mine", action=registry.MODIFY).snapshot()

    assert snapshot["project_id"] == "mine"
    assert snapshot["project_allowed_actions"] == "inspect,modify"
    assert snapshot["project_budget_class"] == "standard"


def test_the_shipped_registry_describes_this_repository(tmp_path: Path) -> None:
    """Dogfood: the engine's own entry must actually resolve, or the first real
    remote submission discovers the file was never valid."""
    engine_root = Path(__file__).resolve().parents[1]

    project = registry.load(engine_root)["ai-platform"]

    assert project.path == engine_root
    assert project.permits(registry.MODIFY)
    assert not project.permits(registry.OPEN_PR)  # not implemented yet (#33)
