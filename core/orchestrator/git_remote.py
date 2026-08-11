"""Git remote synchronization and delivery guards (issue #33).

A run gets one immutable :class:`BaseSnapshot` before its integration
worktree is created. Fetching updates only remote-tracking refs; it never
checks out, resets, merges, or writes the user's working tree. Delivery
helpers verify that the recorded remote base is still current before a caller
with an explicit approval pushes a branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import git

from core.errors import ConfigError
from core.orchestrator import registry


class RemoteSyncError(ConfigError):
    """A remote/base policy could not be satisfied safely."""


@dataclass(frozen=True)
class BaseSnapshot:
    """Immutable Git identity captured at admission time."""

    base_ref: str
    base_sha: str
    remote_url: str = ""
    remote_name: str = ""
    remote_ref: str = ""
    remote_sha: str = ""
    base_branch: str = ""
    fetch_timestamp: str = ""
    sync_policy: str = registry.SYNC_OFFLINE
    sync_status: str = "offline"

    def metadata(self) -> dict[str, str]:
        return {
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "remote_url": _safe_url(self.remote_url),
            "remote_name": self.remote_name,
            "remote_ref": self.remote_ref,
            "remote_sha": self.remote_sha,
            "base_branch": self.base_branch,
            "fetch_timestamp": self.fetch_timestamp,
            "sync_policy": self.sync_policy,
            "sync_status": self.sync_status,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_url(url: str) -> str:
    """Remove userinfo before a URL reaches diagnostics or telemetry."""
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        if parsed.username or parsed.password:
            host = parsed.hostname or ""
            if parsed.port:
                host = f"{host}:{parsed.port}"
            return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
    except ValueError:
        return "<invalid remote URL>"
    return url


def _remote_name(repo: git.Repo, remote_url: str) -> str:
    for remote in repo.remotes:
        if remote_url in set(remote.urls):
            return remote.name
    raise RemoteSyncError(
        f"Configured remote {_safe_url(remote_url)!r} is not present in the target repository"
    )


def _sha(repo: git.Repo, ref: str) -> str | None:
    try:
        return repo.git.rev_parse(ref).strip()
    except git.GitCommandError:
        return None


def _ancestor(repo: git.Repo, ancestor: str, descendant: str) -> bool:
    try:
        return repo.git.merge_base(ancestor, descendant).strip() == ancestor
    except git.GitCommandError:
        return False


def _error_kind(message: str) -> str:
    lower = message.lower()
    if any(token in lower for token in ("could not resolve host", "network is unreachable", "timed out", "connection")):
        return "network"
    if any(token in lower for token in ("authentication", "username", "password", "permission denied", "access denied")):
        return "credentials"
    return "git"


def _fetch(repo: git.Repo, remote_name: str, base_branch: str) -> None:
    refspec = f"+refs/heads/{base_branch}:refs/remotes/{remote_name}/{base_branch}"
    try:
        # This updates only refs/remotes/<remote>/<branch>; the active branch
        # and index are never checked out or reset.
        repo.git.fetch("--no-tags", remote_name, refspec)
    except git.GitCommandError as exc:
        message = str(exc.stderr or exc)
        raise RemoteSyncError(
            f"Git fetch failed ({_error_kind(message)}): {message.strip() or 'unknown error'}"
        ) from exc


def _local_ref(repo: git.Repo, base_branch: str) -> tuple[str, str]:
    if base_branch:
        ref = f"refs/heads/{base_branch}"
        sha = _sha(repo, ref)
        if sha:
            return base_branch, sha
        raise RemoteSyncError(f"Configured base branch {base_branch!r} does not exist locally")
    try:
        return repo.active_branch.name, repo.head.commit.hexsha
    except TypeError:
        return "HEAD", repo.head.commit.hexsha


def synchronize_base(repo: git.Repo, project=None) -> BaseSnapshot:
    """Resolve and pin the base according to a project sync policy."""
    policy = getattr(project, "sync_policy", registry.SYNC_OFFLINE) if project else registry.SYNC_OFFLINE
    if policy not in registry.SYNC_POLICIES:
        raise RemoteSyncError(f"Unknown Git sync policy {policy!r}")

    base_branch = getattr(project, "base_branch", "") if project else ""
    remote_url = getattr(project, "remote", "") if project else ""
    timestamp = _now()

    if policy == registry.SYNC_OFFLINE:
        base_ref, base_sha = _local_ref(repo, base_branch)
        status = "detached_head" if base_ref == "HEAD" else "offline"
        return BaseSnapshot(
            base_ref=base_ref,
            base_sha=base_sha,
            remote_url=_safe_url(remote_url),
            base_branch=base_branch,
            fetch_timestamp=timestamp,
            sync_policy=policy,
            sync_status=status,
        )

    if not remote_url or not base_branch:
        raise RemoteSyncError(
            f"Git sync policy {policy!r} requires both remote and base_branch"
        )

    remote_name = _remote_name(repo, remote_url)
    local_ref, local_sha = _local_ref(repo, base_branch)
    remote_ref = f"refs/remotes/{remote_name}/{base_branch}"
    previous_remote_sha = _sha(repo, remote_ref)
    _fetch(repo, remote_name, base_branch)
    remote_sha = _sha(repo, remote_ref)
    if not remote_sha:
        raise RemoteSyncError(
            f"Remote {remote_name!r} has no branch {base_branch!r}; cannot pin a base"
        )

    force_pushed = bool(
        previous_remote_sha
        and previous_remote_sha != remote_sha
        and not _ancestor(repo, previous_remote_sha, remote_sha)
    )
    if local_sha == remote_sha:
        status = "up_to_date"
        selected_ref, selected_sha = remote_ref, remote_sha
    elif _ancestor(repo, local_sha, remote_sha):
        if policy == registry.SYNC_REQUIRE_UP_TO_DATE:
            raise RemoteSyncError(
                f"Base branch {base_branch!r} is behind remote {remote_name!r}; "
                "update the local checkout or use sync_policy=fetch"
            )
        status = "remote_ahead"
        selected_ref, selected_sha = remote_ref, remote_sha
    elif _ancestor(repo, remote_sha, local_sha):
        if policy == registry.SYNC_REQUIRE_UP_TO_DATE:
            raise RemoteSyncError(
                f"Base branch {base_branch!r} has local commits not on remote {remote_name!r}; "
                "push or rebase before starting a remote run"
            )
        status = "local_ahead"
        selected_ref, selected_sha = local_ref, local_sha
    else:
        status = "remote_force_pushed" if force_pushed else "diverged"
        raise RemoteSyncError(
            f"Base branch {base_branch!r} diverged from remote {remote_name!r}"
            f" ({status}); rebase or reconcile it before starting the run"
        )

    return BaseSnapshot(
        base_ref=selected_ref,
        base_sha=selected_sha,
        remote_url=_safe_url(remote_url),
        remote_name=remote_name,
        remote_ref=remote_ref,
        remote_sha=remote_sha,
        base_branch=base_branch,
        fetch_timestamp=timestamp,
        sync_policy=policy,
        sync_status=status,
    )


def verify_base_current(repo: git.Repo, snapshot: BaseSnapshot) -> None:
    """Ensure the remote base is unchanged since the run was admitted."""
    if not snapshot.remote_url or not snapshot.base_branch or not snapshot.remote_sha:
        return
    try:
        output = repo.git.ls_remote("--heads", snapshot.remote_url, snapshot.base_branch)
    except git.GitCommandError as exc:
        message = str(exc.stderr or exc)
        raise RemoteSyncError(
            f"Cannot verify remote base ({_error_kind(message)}): {message.strip() or 'unknown error'}"
        ) from exc
    current = output.split()[0] if output.split() else ""
    if current != snapshot.remote_sha:
        raise RemoteSyncError(
            f"Remote base {snapshot.base_branch!r} moved from {snapshot.remote_sha[:12]} "
            f"to {current[:12] or 'missing'}; rebase is required before delivery"
        )


def push_delivery_branch(
    repo: git.Repo, snapshot: BaseSnapshot, branch: str, *, approved: bool = False
) -> str:
    """Push a delivery branch only after approval and base revalidation."""
    if not approved:
        raise RemoteSyncError("Pushing a delivery branch requires explicit approval")
    if not snapshot.remote_url or not snapshot.remote_name:
        raise RemoteSyncError("Cannot push without a synchronized project remote")
    verify_base_current(repo, snapshot)
    try:
        repo.git.push(
            snapshot.remote_name,
            f"refs/heads/{branch}:refs/heads/{branch}",
            "--no-force",
        )
    except git.GitCommandError as exc:
        message = str(exc.stderr or exc)
        raise RemoteSyncError(
            f"Git push failed ({_error_kind(message)}): {message.strip() or 'unknown error'}"
        ) from exc
    return f"refs/heads/{branch}"
