# Managed local user service

Issue #40 adds an optional, local-only service around the durable queue. The
service core is portable and the repository provides three host profiles:

| Profile | Supervisor | Template | Host-specific step |
|---|---|---|---|
| Linux | systemd user | deploy/systemd/ | systemd is normally already enabled |
| WSL2 | systemd user | deploy/systemd/ | enable systemd in /etc/wsl.conf |
| macOS | launchd user agent | deploy/macos/ | replace /Users/REPLACE in the plist |

None of these profiles exposes REST/SSE or accepts remote commands. OpenClaw
and the REST/SSE transport remain separate adapters.

## Common behavior

All profiles run as the logged-in user with the same service-health and
service-run commands. Readiness checks the engine root, required mount paths,
SQLite integrity and WAL mode, worker importability, optional network endpoint
and provider CLI state. A FAIL prevents processing; provider WARNs are allowed
when another provider is authenticated.

service-run uses bounded exponential backoff and resets it after a successful
cycle. SIGTERM/SIGINT stops after the current cycle; a hard kill is reconciled
as interrupted on the next startup. The systemd profile uses KillMode=mixed and
a 120-second timeout; launchd uses KeepAlive and ThrottleInterval.

Use AI_PLATFORM_SERVICE_REQUIRED_PATHS for a path-separated mount list,
AI_PLATFORM_SERVICE_NETWORK=host:port for a concrete network prerequisite,
AI_PLATFORM_SERVICE_MAX_BACKOFF for the retry ceiling, and
AI_PLATFORM_SERVICE_LOG for a log file. The CLI --env-file accepts simple
KEY=VALUE lines only; it never evaluates shell code. Because passing `--env-file` is explicit, its values override inherited environment settings. Keep the file mode 0600 and put provider credentials in the provider CLI's own user configuration.

## Linux and WSL2 (systemd)

Copy deploy/systemd/*.service and the timer to ~/.config/systemd/user/. Set
the working directory and uv path if the checkout is not
~/workspace/ai-platform. Enable and start:

    loginctl enable-linger $USER
    systemctl --user daemon-reload
    systemctl --user enable --now ai-platform.service ai-platform-backup.timer

For WSL2 only, add [boot] systemd=true to /etc/wsl.conf, run wsl --shutdown
from Windows, then reopen the distribution. Verify uv --version and
ai-platform doctor before starting.

## macOS (launchd)

Create the log directory and replace /Users/REPLACE in both plist templates:

    mkdir -p ~/Library/Logs/ai-platform ~/Library/LaunchAgents
    sed -i '' "s#/Users/REPLACE#$HOME#g" deploy/macos/com.ai-platform.*.plist

Copy the resulting files to ~/Library/LaunchAgents and load them for the
current user:

    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ai-platform.worker.plist
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ai-platform.backup.plist
    launchctl kickstart -k gui/$(id -u)/com.ai-platform.worker

To stop and remove them, use launchctl bootout with the same labels. launchd
has no systemd-style mount ordering; encode required paths in the readiness
probe and let service-run wait with bounded backoff.

## Health, logs and recovery

service-health --json is stable for scripts. Logs use restrictive permissions
and rotation; inspect systemd logs with journalctl --user -u ai-platform or
the configured file, and launchd logs under ~/Library/Logs/ai-platform.

The daily backup profile runs ai-platform backup --keep 7. Backups contain
engine SQLite databases, use sqlite3.Connection.backup (WAL-aware), integrity
checks, checksums, atomic publication and retention. Stop the worker before a
restore:

    ai-platform restore /path/to/snapshot
    ai-platform service-health

Restore refuses while active jobs exist unless --force is explicitly supplied.
Keep snapshots off the same disk and perform periodic restore drills.

## Upgrade and rollback

Stop the user service, update the checkout and uv lockfile, run focused and
full tests, then restart and verify readiness. Roll back by restoring the last
known-good commit and lockfile. If schema migration fails, restore the last
snapshot while stopped and let startup reconciliation mark stale jobs
interrupted.

The worker contract is OS-portable; supervisor configuration, filesystem
mounts, credential locations and ACLs remain host-specific.
