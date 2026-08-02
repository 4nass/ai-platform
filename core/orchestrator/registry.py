"""Which repositories exist, and what may be done to each.

`--repo /any/path` is a reasonable interface for someone standing at their own
workstation. It is an unacceptable one for a request that arrived as chat text
from a phone: the path would be attacker-chosen, and "index this repo, run its
test command, let agents write to it" is not something to do to an arbitrary
directory. Issue [#25](https://github.com/4nass/ai-platform/issues/25).

So a remote caller never names a path. It names a **project id**, and the
engine — not the caller — decides what that id means. The registry is engine
owned (`config/projects.yaml`, beside the engine's own config, not inside any
target), which is what makes it a trust boundary rather than a convenience
lookup.

**Why a separate file from `platform.yaml`.** ADR-008 collapsed six engine
config files into one because they were one policy surface fragmented six
ways. This is not that surface. `platform.yaml` is tuning — profile, gates,
budgets — and re-tuning it changes how well runs go. This is inventory *and*
an allowlist, where a mistake changes what can be reached at all. Different
lifecycle (edited when you add a repo, not when you tune), different blast
radius, and small enough to audit at a glance. Recorded in ADR-010.

**Canonicalization is the whole security argument.** `Path.resolve()` collapses
`..`, duplicate separators and — crucially — symlinks, so a registry entry
cannot point outside `roots` by way of a link planted anywhere along its path.
The check is then a parent test on the *resolved* path, never a string prefix:
`/home/anass/workspace-evil` starts with `/home/anass/workspace` as text and is
not under it as a directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import git
import yaml

from core.errors import ConfigError

CONFIG_PATH = Path("config/projects.yaml")

INSPECT = "inspect"
"""Read-only: select context, explain routing, read history. Never writes."""

MODIFY = "modify"
"""Run the DAG: create branches and worktrees, let agents write, commit."""

TEST = "test"
"""Execute the target's own declared test command (see target_config)."""

OPEN_PR = "open_pr"
"""Publish: push a branch and open a pull request. Not implemented yet — it is
declarable so a project can withhold it before the capability exists, rather
than being granted it retroactively the day it ships."""

ACTIONS = (INSPECT, MODIFY, TEST, OPEN_PR)

DEFAULT_ACTIONS = (INSPECT,)
"""What a project gets by declaring nothing. Read-only on purpose: an entry
someone added in a hurry should be the least dangerous thing in the file, not
the most."""

DEFAULT_BUDGET_CLASS = "standard"
"""Which declared budget applies to this project's runs (issue #27). A name,
not a number, so the amounts live with the rest of the budget policy and a
project only says which class it belongs to."""


class RegistryError(ConfigError):
    """An unknown project, a disallowed action, or a target that no longer
    matches what the registry says it is.

    A `ConfigError` subclass so existing CLI handling reports it the same way,
    distinct so an admission failure can be told from a malformed config —
    they are read by different people for different reasons.
    """


@dataclass(frozen=True)
class Project:
    """One allowlisted repository and the policy attached to it."""

    id: str
    path: Path
    """Already canonical: resolved, symlinks followed, verified under `roots`.
    Callers may use it directly — re-resolving would be a second chance to get
    it wrong."""

    remote: str = ""
    base_branch: str = ""
    allowed_actions: tuple[str, ...] = DEFAULT_ACTIONS
    budget_class: str = DEFAULT_BUDGET_CLASS
    approval_required: tuple[str, ...] = ()
    """Actions this project will not perform without an explicit human decision
    (issue #28), even when `allowed_actions` permits them."""

    def permits(self, action: str) -> bool:
        return action in self.allowed_actions

    def snapshot(self) -> dict:
        """The policy as recorded on the run that used it.

        A run is only interpretable against the policy in force when it
        started: "this run pushed a branch" means something different if
        `open_pr` was allowed than if it was not, and the file can be edited
        afterwards. Same reasoning as target_config's frozen read.
        """
        return {
            "project_id": self.id,
            "project_remote": self.remote,
            "project_base_branch": self.base_branch,
            "project_allowed_actions": ",".join(self.allowed_actions),
            "project_budget_class": self.budget_class,
            "project_approval_required": ",".join(self.approval_required),
        }


def _as_tuple(value, *, field_name: str, project_id: str, valid: tuple[str, ...] | None = None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise RegistryError(
            f"Project {project_id!r}: {field_name} must be a list, got {type(value).__name__}"
        )
    items = tuple(str(item) for item in value)
    if valid is not None:
        unknown = [item for item in items if item not in valid]
        if unknown:
            raise RegistryError(
                f"Project {project_id!r}: unknown {field_name} {', '.join(unknown)}. "
                f"Valid: {', '.join(valid)}"
            )
    return items


def _roots(data: dict, engine_root: Path) -> tuple[Path, ...]:
    """Directories every project path must live under.

    Declared rather than inferred, and required once any project is: the
    containment check is the allowlist's teeth, and a default of "anywhere"
    would quietly remove them.
    """
    declared = data.get("roots")
    if not declared:
        return ()
    if isinstance(declared, str) or not isinstance(declared, (list, tuple)):
        raise RegistryError(f"roots must be a list, got {type(declared).__name__}")
    return tuple(Path(str(root)).expanduser().resolve() for root in declared)


def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    """Whether a *resolved* path is inside one of the roots.

    `Path.is_relative_to`, not a string prefix: `/srv/workspace-evil` starts
    with `/srv/workspace` as text and is a sibling of it as a directory. Both
    sides are resolved before this is called, so a symlink cannot make an
    outside path look inside.
    """
    return any(path == root or path.is_relative_to(root) for root in roots)


def _project(project_id: str, raw: dict, roots: tuple[Path, ...]) -> Project:
    if not isinstance(raw, dict):
        raise RegistryError(f"Project {project_id!r} must be a mapping, got {type(raw).__name__}")

    declared_path = raw.get("path")
    if not declared_path:
        raise RegistryError(f"Project {project_id!r} declares no path")

    # expanduser before resolve so `~` is a supported spelling rather than a
    # literal directory name; resolve then collapses `..`, repeated separators
    # and every symlink on the way down.
    path = Path(str(declared_path)).expanduser().resolve()

    if not roots:
        raise RegistryError(
            f"Project {project_id!r} is declared but {CONFIG_PATH} has no `roots`. "
            "Every project path must be inside a declared root — without one there is "
            "no allowlist, only a list."
        )
    if not _is_within(path, roots):
        # The resolved path is reported, not the declared one: when a symlink
        # is what escaped, the declared spelling is exactly the misleading half.
        raise RegistryError(
            f"Project {project_id!r} resolves to {path}, which is outside every declared "
            f"root ({', '.join(str(root) for root in roots)}). Declared as {declared_path!r}; "
            "if those differ, a symlink or `..` in the path is the reason."
        )

    return Project(
        id=project_id,
        path=path,
        remote=str(raw.get("remote") or ""),
        base_branch=str(raw.get("base_branch") or ""),
        allowed_actions=_as_tuple(
            raw.get("allowed_actions", list(DEFAULT_ACTIONS)),
            field_name="allowed_actions",
            project_id=project_id,
            valid=ACTIONS,
        )
        or DEFAULT_ACTIONS,
        budget_class=str(raw.get("budget_class") or DEFAULT_BUDGET_CLASS),
        approval_required=_as_tuple(
            raw.get("approval_required"),
            field_name="approval_required",
            project_id=project_id,
            valid=ACTIONS,
        ),
    )


def load(engine_root: Path) -> dict[str, Project]:
    """Every allowlisted project, keyed by id.

    An absent file is an empty registry, not an error: the local CLI works
    entirely through `--repo` and has no reason to declare anything. What is
    *not* allowed is an absent file silently permitting a project id — that is
    `resolve`'s job, and it refuses.
    """
    path = engine_root / CONFIG_PATH
    if not path.is_file():
        return {}

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RegistryError(f"{CONFIG_PATH} must be a mapping, got {type(data).__name__}")

    roots = _roots(data, engine_root)
    declared = data.get("projects") or {}
    if not isinstance(declared, dict):
        raise RegistryError(f"{CONFIG_PATH}: `projects` must be a mapping of id -> project")

    return {str(pid): _project(str(pid), raw, roots) for pid, raw in declared.items()}


def verify(project: Project) -> None:
    """Checks the target is still the repository the registry describes.

    Separate from `load` because it touches the filesystem and git, and a
    command listing the registry should not have to. Called by `resolve`, so
    the admission path always pays for it.

    The remote and base branch are checked because a path is not an identity:
    a directory can be replaced, re-cloned from somewhere else, or be a
    different checkout than the one whose policy was written. `remote` and
    `base_branch` are each optional — an unset one is not checked rather than
    being checked against the empty string, so a purely local project is
    expressible without inventing a remote for it.
    """
    if not project.path.is_dir():
        raise RegistryError(f"Project {project.id!r}: {project.path} does not exist")

    try:
        repo = git.Repo(project.path)
    except Exception as exc:
        raise RegistryError(
            f"Project {project.id!r}: {project.path} is not a git repository "
            f"({type(exc).__name__})"
        ) from None

    if project.remote:
        actual = {url for remote in repo.remotes for url in remote.urls}
        if project.remote not in actual:
            raise RegistryError(
                f"Project {project.id!r} expects remote {project.remote}, but "
                f"{project.path} has {', '.join(sorted(actual)) or 'none'}. Refusing: the "
                "path is the same, the repository is not."
            )

    if project.base_branch and project.base_branch not in {head.name for head in repo.heads}:
        raise RegistryError(
            f"Project {project.id!r} declares base branch {project.base_branch!r}, which "
            f"does not exist in {project.path}."
        )


def resolve(engine_root: Path, project_id: str, *, action: str = INSPECT) -> Project:
    """The project this id names, if it is allowlisted and permits `action`.

    The single entry point admission goes through, and the reason it is one
    function rather than a lookup plus a check at each call site: an
    unauthorized action must fail *before* anything is indexed, any provider
    is chosen, or any worktree exists — and a caller that forgot the second
    half would not fail at all.

    Unknown ids do not report what does exist. A registry listing is inventory
    of the owner's machine, and probing for valid ids is the first thing an
    unauthorized caller would do.
    """
    if action not in ACTIONS:
        raise RegistryError(f"Unknown action {action!r}. Valid: {', '.join(ACTIONS)}")

    project = load(engine_root).get(project_id)
    if project is None:
        raise RegistryError(
            f"No project {project_id!r} in {CONFIG_PATH}. A project must be declared there "
            "before it can be operated on by id."
        )

    if not project.permits(action):
        raise RegistryError(
            f"Project {project_id!r} does not permit {action!r} "
            f"(allowed: {', '.join(project.allowed_actions)})"
        )

    verify(project)
    return project
