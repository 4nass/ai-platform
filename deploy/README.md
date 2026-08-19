# Managed local user service profiles

The service core is portable across Linux, WSL2 and macOS. Select the host
profile that matches the runtime:

- systemd/: Linux native and WSL2 user units. Linux uses systemd directly;
  WSL2 additionally requires systemd enabled in /etc/wsl.conf.
- macos/: launchd user agents. Replace /Users/REPLACE with the real home
  directory before loading the plist files.

All profiles run as the logged-in user, use the same service-health and
service-run commands, and keep the worker local-only. The profiles are
templates: adjust the checkout path, uv path, environment file and log
locations for the host. Operational details are in docs/service-operations.md.
