"""Preflight diagnostics for a reliable local run.

The doctor deliberately reports a small, stable vocabulary instead of exposing
provider-specific exceptions to the CLI:

* ``PASS`` means the prerequisite is present and usable;
* ``WARN`` means an optional capability is absent or degraded;
* ``FAIL`` means a normal run cannot be expected to complete reliably.

Checks are read-only. Provider authentication commands are the same cheap
status probes used by the adapters; no model call is made.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import git

from core.errors import ConfigError

Status = Literal["PASS", "WARN", "FAIL"]


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    remediation: str = ""


@dataclass(frozen=True)
class Report:
    checks: tuple[Check, ...]

    @property
    def failed(self) -> bool:
        return any(check.status == "FAIL" for check in self.checks)


def _command_version(command: str) -> tuple[bool, str]:
    path = shutil.which(command)
    if not path:
        return False, f"{command} is not in PATH"
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{command} could not be queried ({type(exc).__name__})"
    if result.returncode != 0:
        return False, f"{command} --version exited {result.returncode}"
    version = (result.stdout or result.stderr).strip().splitlines()
    return True, version[0] if version else f"{command} is available"


def _python_check() -> Check:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 11):
        return Check("Python >= 3.11", "PASS", version)
    return Check("Python >= 3.11", "FAIL", f"{version} is too old", "Install Python 3.11 or newer")


def _uv_candidates() -> tuple[Path, ...]:
    """Common install locations checked when ``uv`` is not in PATH."""
    home = Path.home()
    return (
        home / ".local" / "bin" / "uv",
        home / ".cargo" / "bin" / "uv",
        Path("/usr/local/bin/uv"),
        Path("/usr/bin/uv"),
    )


def _uv_check() -> Check:
    ok, detail = _command_version("uv")
    if ok:
        return Check("uv", "PASS", detail)

    if not shutil.which("uv"):
        installed = next(
            (path for path in _uv_candidates() if path.is_file() and os.access(path, os.X_OK)),
            None,
        )
        if installed is not None:
            directory = str(installed.parent)
            export = f'export PATH="{directory}:$PATH"'
            persist = f"echo '{export}' >> ~/.bashrc\nsource ~/.bashrc"
            return Check(
                "uv",
                "FAIL",
                f"uv is installed at {installed}, but that directory is not in PATH",
                f"Current shell: {export}\nPersist Bash/WSL: {persist}",
            )
        return Check(
            "uv",
            "FAIL",
            "uv is not in PATH and no common installation was found",
            "Install: curl -LsSf https://astral.sh/uv/install.sh | sh\nThen open a new shell and rerun `ai-platform doctor`",
        )

    return Check(
        "uv",
        "FAIL",
        detail,
        "Repair or reinstall uv, then rerun `uv --version`",
    )


def _tool_check(command: str, *, required: bool = True) -> Check:
    if command == "uv":
        return _uv_check()
    ok, detail = _command_version(command)
    if ok:
        return Check(command, "PASS", detail)
    status: Status = "FAIL" if required else "WARN"
    remediation = f"Install {command} and ensure it is in PATH" if required else "Install it if this capability is needed"
    return Check(command, status, detail, remediation)


def _config_check(engine_root: Path) -> Check:
    from core.orchestrator import platform_config

    try:
        config = platform_config.load(engine_root)
        profile = platform_config.profile_preset_path(engine_root, config.profile)
        workflow = platform_config.workflow_preset_path(engine_root, config.workflow_mode)
        context = platform_config.context_preset_path(engine_root, config.context_mode)
    except Exception as exc:
        return Check(
            "Engine configuration",
            "FAIL",
            str(exc),
            "Fix config/platform.yaml or the selected preset before running",
        )
    for path in (profile, workflow, context):
        if not path.is_file():
            return Check("Engine configuration", "FAIL", f"Missing preset: {path}")
    return Check(
        "Engine configuration",
        "PASS",
        f"profile={config.profile}, workflow={config.workflow_mode}, context={config.context_mode}",
    )


def _provider_checks(engine_root: Path) -> list[Check]:
    """Report every declared CLI provider plus an aggregate readiness check."""
    from core.orchestrator import platform_config, router
    from providers.claude_code import adapter as claude_code
    from providers.codex_cli import adapter as codex_cli

    adapters = {"claude_code": claude_code, "codex_cli": codex_cli}
    declared: set[str] = set()
    try:
        config = platform_config.load(engine_root)
        for role in _profile_roles(engine_root, config.profile):
            for complexity in router.COMPLEXITIES:
                declared.update(
                    profile.provider
                    for profile in router.eligible_profiles(
                        engine_root, role, complexity, profile=config.profile
                    )
                )
    except Exception as exc:
        return [Check("Provider readiness", "FAIL", f"Cannot inspect provider policy: {exc}")]

    checks: list[Check] = []
    healthy = 0
    for provider in sorted(declared):
        adapter = adapters.get(provider)
        if adapter is None:
            checks.append(
                Check(
                    f"Provider {provider}",
                    "FAIL",
                    "Declared provider has no installed adapter",
                    "Remove it from the active profile or implement its adapter",
                )
            )
            continue
        if not shutil.which("claude" if provider == "claude_code" else "codex"):
            checks.append(
                Check(
                    f"Provider {provider}",
                    "WARN",
                    "CLI is not in PATH",
                    "Install the CLI and authenticate it",
                )
            )
            continue
        error = adapter._check_auth()
        if error:
            checks.append(Check(f"Provider {provider}", "WARN", error, "Authenticate the provider CLI"))
        else:
            healthy += 1
            checks.append(Check(f"Provider {provider}", "PASS", "CLI available and authenticated"))

    if healthy:
        checks.append(Check("At least one provider", "PASS", f"{healthy} authenticated provider(s) available"))
    else:
        checks.append(
            Check(
                "At least one provider",
                "FAIL",
                "No configured provider is available and authenticated",
                "Run `codex login` or `claude auth login`",
            )
        )
    return checks


def _profile_roles(engine_root: Path, profile: str) -> set[str]:
    import yaml

    from core.orchestrator import platform_config

    path = platform_config.profile_preset_path(engine_root, profile)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Profile preset {path} must be a mapping")
    return {str(role) for role in data}


def _target_checks(target_root: Path) -> list[Check]:
    checks: list[Check] = []
    if not target_root.exists() or not target_root.is_dir():
        return [
            Check(
                "Target repository",
                "FAIL",
                f"Directory does not exist: {target_root}",
                "Pass --repo with an existing Git repository",
            )
        ]
    try:
        repo = git.Repo(target_root)
    except (git.InvalidGitRepositoryError, git.NoSuchPathError, OSError) as exc:
        return [
            Check(
                "Target repository",
                "FAIL",
                f"Not a Git repository ({type(exc).__name__})",
                "Initialize Git or point --repo at the target checkout",
            )
        ]
    checks.append(Check("Target repository", "PASS", f"Git repository at {target_root}"))

    from core.orchestrator import target_config

    try:
        policy = target_config.load_at_commit(repo, repo.head.commit.hexsha)
    except Exception as exc:
        return checks + [
            Check(
                "Target validation policy",
                "FAIL",
                str(exc),
                "Fix the committed .ai-platform.yml",
            )
        ]
    if policy.test_command is None:
        checks.append(
            Check(
                "Target test command",
                "WARN",
                "No committed test_command; validation will be skipped",
                "Add test_command to .ai-platform.yml",
            )
        )
    else:
        executable = shutil.which(policy.test_command[0])
        if executable:
            checks.append(Check("Target test command", "PASS", " ".join(policy.test_command)))
        else:
            checks.append(
                Check(
                    "Target test command",
                    "FAIL",
                    f"Executable {policy.test_command[0]!r} is not in PATH",
                    "Install the declared test runner or correct .ai-platform.yml",
                )
            )
        if policy.test_sandbox:
            if shutil.which("bwrap"):
                checks.append(Check("Test sandbox", "PASS", "Bubblewrap available"))
            else:
                checks.append(
                    Check(
                        "Test sandbox",
                        "WARN",
                        "Bubblewrap is not installed; tests will run unsandboxed",
                        "Install bwrap before unattended runs",
                    )
                )
    if repo.is_dirty(untracked_files=True):
        checks.append(
            Check(
                "Target working tree",
                "WARN",
                "Working tree is dirty; default runs use committed HEAD",
                "Commit changes or use the documented dirty-tree policy",
            )
        )
    else:
        checks.append(Check("Target working tree", "PASS", "Clean"))
    return checks


def run(engine_root: Path, target_root: Path | None = None) -> Report:
    """Run all read-only preflight checks for the engine and target."""
    checks: list[Check] = [_python_check(), _tool_check("uv"), _tool_check("git")]
    checks.append(_config_check(engine_root))
    checks.extend(_provider_checks(engine_root))
    checks.extend(_target_checks((target_root or Path.cwd()).resolve()))
    return Report(tuple(checks))
