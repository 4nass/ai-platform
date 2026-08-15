# WSL2 user service

These units run the queue locally under the logged-in Unix user. They do not expose a network listener.

1. Enable systemd in /etc/wsl.conf:

   [boot]
   systemd=true

   Then run wsl --shutdown from Windows and reopen the distribution.
2. Install uv in ~/.local/bin and verify uv --version.
3. Copy the units to ~/.config/systemd/user/, create
   ~/.config/ai-platform/service.env with mode 0600, and adjust the working
   directory if the repository is elsewhere.
4. Enable lingering and start:

   loginctl enable-linger $USER
   systemctl --user daemon-reload
   systemctl --user enable --now ai-platform.service ai-platform-backup.timer

The service waits for local filesystem mounts and (when configured) a network
endpoint before processing jobs. Restart backoff is bounded by
AI_PLATFORM_SERVICE_MAX_BACKOFF. Inspect systemctl --user status ai-platform
and ai-platform service-health --json. See docs/service-operations.md for
upgrades, rollback and disaster recovery.
