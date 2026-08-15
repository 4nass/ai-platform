# Managed service operations

Issue #40 adds an optional, local-only WSL2/systemd user service. It consumes the
durable queue; it does not expose REST/SSE and it never accepts remote commands.

## Minimal installation

The host needs WSL2 with systemd enabled, Python/uv available to the Unix user,
and the repository checkout. Enable [boot] systemd=true in /etc/wsl.conf,
run wsl --shutdown, then verify uv --version and ai-platform doctor.
Copy deploy/systemd/*.service and the timer to
~/.config/systemd/user/. Set chmod 600 ~/.config/ai-platform/service.env.
Only non-secret settings belong there; provider credentials remain in the
provider CLI's user configuration and are never logged.

Enable the user manager with loginctl enable-linger $USER, then run
systemctl --user daemon-reload and
systemctl --user enable --now ai-platform.service ai-platform-backup.timer.
The unit waits for local mounts and network-online.target; an optional
AI_PLATFORM_SERVICE_NETWORK=host:port makes readiness verify a concrete
endpoint. AI_PLATFORM_SERVICE_REQUIRED_PATHS accepts a colon-separated list.

## Health and lifecycle

ai-platform service-health reports PASS/WARN/FAIL checks locally. --json is
stable for scripts; a FAIL means the worker must not process jobs. Provider
WARNs are allowed when another provider is authenticated. service-run uses a
bounded exponential backoff (AI_PLATFORM_SERVICE_MAX_BACKOFF) and resets it
after a successful cycle. SIGTERM/SIGINT stops after the current cycle; a hard
kill is reconciled as interrupted on the next startup. KillMode=mixed and a
120-second stop timeout keep process groups predictable.

Logs are written to ~/.local/state/ai-platform/service.log with restrictive
permissions and rotation. Inspect them with
journalctl --user -u ai-platform.service or the configured file. Never put
tokens or provider output in the environment file.

## Backup, restore and recovery

The daily timer runs ai-platform backup --keep 7. Backups contain only the
engine SQLite databases, are WAL-aware (sqlite3.Connection.backup), integrity
checked, checksummed and atomically published with mode 0700/0600. Create one
manually with ai-platform backup --destination /path.

Stop the worker before a restore:

    systemctl --user stop ai-platform.service
    ai-platform restore /path/to/snapshot
    ai-platform service-health
    systemctl --user start ai-platform.service

Restore refuses while queued/running/approval jobs exist unless --force is
explicitly supplied. Keep snapshots off the same disk for disaster recovery;
the manifest checksum detects incomplete or tampered copies.

## Upgrade and rollback

git fetch and review the target branch, stop the user service, install/update
dependencies with uv, run the focused and full test suites, then start the
service and verify readiness. A failed deploy is rolled back by checking out the
last known-good commit, reinstalling its lockfile, and restarting. Queue state
is preserved by SQLite backups; if schema migration fails, restore the last
snapshot while stopped and reconcile interrupted jobs after restart.

Host-level WSL systemd setup is intentionally operator-specific and remains a
deployment concern, not a remote-control dependency.
