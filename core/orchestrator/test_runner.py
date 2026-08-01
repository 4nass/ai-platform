"""Runs the target repo's test suite after a modification.

The command is declared by the target repo itself (.ai-platform.yml), not
assumed: "uv run pytest -q" is only true for this repo, and a `--repo`
target using npm, go test, cargo, etc. would otherwise get a meaningless
failure against a command that doesn't even apply to it.

Tests are agent-written code, not just agent-*reviewed* code (see issue #4):
they run before any review verdict exists, in the parent process, with the
invoking user's own privileges, on the merged repo. Sandboxed by default via
bubblewrap (`bwrap`) when it's available -- no network, and no filesystem
write access outside the repo being tested. This is deliberately not the
same guarantee a container would give (the whole host filesystem is still
visible *read-only*, since the test command's own toolchain -- uv, npm, go,
whatever -- has to keep resolving normally, and there is no per-target image
to know what a given repo needs); it closes the two highest-value risks
(destructive writes elsewhere on the host, and network exfiltration) without
asking for a Docker/Podman dependency this project doesn't otherwise have.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_PATH = Path(".ai-platform.yml")
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_SANDBOX_CACHE_DIRS = ["~/.cache"]
"""Read-write binds beyond the repo itself. `~/.cache` alone covers uv/pip's
default cache locations (this project's own toolchain) because both respect
the XDG convention; a target using cargo/go/npm may need to declare its own
cache dir (~/.cargo, ~/go/pkg/mod, ~/.npm, ...) via sandbox_cache_dirs in its
own .ai-platform.yml -- there is no way to know a target's toolchain needs in
advance, so this is declared, not guessed."""


@dataclass
class TestResult:
    passed: bool
    output: str
    skipped: bool = False
    """True when the target declared no test_command. `passed` is still True
    in that case — skipped is neutral, not a failure — but kept as a
    distinct field so a caller can tell "nothing to fail" from "verified and
    green"."""
    sandboxed: bool = False
    sandbox_warning: str = ""
    """Set when sandboxing was requested (the default) but couldn't be
    applied -- e.g. bwrap isn't installed. Tests still run; degrading loudly
    beats silently running unprotected, the same reasoning as the router's
    never-block guarantee."""


@dataclass
class _Config:
    command: list[str] | None
    timeout: int
    sandbox: bool
    cache_dirs: list[str]
    env: dict[str, str]


def _load_config(repo_root: Path) -> _Config:
    path = repo_root / CONFIG_PATH
    data: dict = {}
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    command = data.get("test_command")
    if isinstance(command, str):
        command = shlex.split(command)
    elif isinstance(command, list) and all(isinstance(part, str) for part in command):
        command = list(command)
    else:
        command = None

    timeout = data.get("test_timeout", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        timeout = DEFAULT_TIMEOUT_SECONDS

    sandbox = data.get("test_sandbox", True)
    if not isinstance(sandbox, bool):
        sandbox = True

    cache_dirs = data.get("sandbox_cache_dirs", DEFAULT_SANDBOX_CACHE_DIRS)
    if not isinstance(cache_dirs, list) or not all(isinstance(d, str) for d in cache_dirs):
        cache_dirs = DEFAULT_SANDBOX_CACHE_DIRS

    env = data.get("test_env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        env = {}

    return _Config(command=command, timeout=int(timeout), sandbox=sandbox, cache_dirs=cache_dirs, env=env)


def _sandbox_command(repo_root: Path, cache_dirs: list[str], env: dict[str, str]) -> list[str]:
    """Wraps a command in a bubblewrap sandbox: the whole host filesystem
    stays visible read-only (so the test command's own toolchain keeps
    resolving), `repo_root` is bound read-write, declared cache dirs are
    bound read-write too, and every namespace is unshared -- no network.

    `--dir` before binding `repo_root` matters specifically when `repo_root`
    lives under /tmp (true of every pytest tmp_path-based fixture, and
    plausible for a real target too): `--tmpfs /tmp` replaces /tmp wholesale
    first, and a bind mount placed at a path that doesn't exist yet in that
    fresh tmpfs silently fails, not auto-creates it. Verified: an otherwise
    identical repro without `--dir` failed with "No such file or directory"
    on `--chdir`, not on the bind step itself, which is what made this easy
    to miss.
    """
    # bwrap's --bind/--chdir need an absolute path: a relative one (e.g. the
    # caller passed Path(".")) doesn't resolve sensibly once the sandbox's
    # own mount namespace is in effect. Caught for real: with a relative
    # path this failed as "execvp uv: No such file or directory" -- PATH
    # lookup failing downstream of a chdir that silently landed somewhere
    # other than intended, not an error on the bind/chdir calls themselves.
    repo = str(repo_root.resolve())
    cmd = [
        "bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--ro-bind", "/", "/",
        "--tmpfs", "/tmp",
        "--dir", repo,
        "--bind", repo, repo,
        "--dev", "/dev",
        "--proc", "/proc",
    ]
    for raw in cache_dirs:
        expanded = str(Path(raw).expanduser())
        cmd += ["--bind-try", expanded, expanded]
    for key, value in env.items():
        cmd += ["--setenv", key, value]
    cmd += ["--chdir", repo, "--"]
    return cmd


def run_tests(repo_root: Path) -> TestResult:
    config = _load_config(repo_root)
    if config.command is None:
        return TestResult(
            passed=True,
            skipped=True,
            output=f"No test_command declared in {CONFIG_PATH} — tests skipped.",
        )

    sandbox_warning = ""
    sandboxed = False
    command = config.command
    if config.sandbox:
        if shutil.which("bwrap") is not None:
            command = _sandbox_command(repo_root, config.cache_dirs, config.env) + config.command
            sandboxed = True
        else:
            sandbox_warning = (
                "test_sandbox is enabled but `bwrap` isn't installed — tests are running "
                "unsandboxed, with this process's own filesystem and network access."
            )

    # Only used when NOT sandboxed: bwrap gets env vars via --setenv above,
    # scoped to the sandboxed process rather than this one.
    run_kwargs = {}
    if not sandboxed and config.env:
        run_kwargs["env"] = {**os.environ, **config.env}

    try:
        proc = subprocess.run(
            command, cwd=repo_root, capture_output=True, text=True, timeout=config.timeout, **run_kwargs
        )
    except subprocess.TimeoutExpired as exc:
        return TestResult(passed=False, output=f"Timeout after {exc.timeout}s", sandboxed=sandboxed)
    except FileNotFoundError as exc:
        return TestResult(passed=False, output=f"test_command not found: {exc}", sandboxed=sandboxed)
    return TestResult(
        passed=proc.returncode == 0,
        output=proc.stdout + proc.stderr,
        sandboxed=sandboxed,
        sandbox_warning=sandbox_warning,
    )


def format_test_summary(result: TestResult) -> str:
    """Formats a `TestResult` into a readable one-line summary (PASS/FAIL/SKIPPED + output)."""
    status = "SKIPPED" if result.skipped else ("PASS" if result.passed else "FAIL")
    output = result.output.strip()
    return f"[{status}] {output}" if output else f"[{status}]"
