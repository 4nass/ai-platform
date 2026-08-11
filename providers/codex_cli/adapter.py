"""Codex CLI provider: `codex exec`, the non-interactive mode.

Shaped like providers.claude_code.adapter — a subprocess that edits files
itself and reports back — but the two CLIs differ in three ways that matter,
each verified against the installed binary (codex-cli 0.146.0) rather than
assumed:

- **Tool restriction is a sandbox, not an allow-list.** `--sandbox read-only`
  is the structural counterpart to claude_code's `--allowedTools`.
- **There is no system-prompt flag.** The role prompt goes into the prompt
  text, as ordinary user-turn content. On claude_code it is privileged via
  `--append-system-prompt`; here it is not, and that is a real behavioural
  difference between the two providers.
- **Token accounting follows the OpenAI convention**, which is not the
  platform's — see _parse_usage.
- **Execution profiles are explicit command overrides.** A selected model is
  passed with `--model`; effort is a TOML string in `model_reasoning_effort`,
  so neither silently falls back to a user's local default.

This module previously refused to guess the interface. It no longer has to.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from providers.base import AgentTask, ProviderResult, TokenUsage, load_role_prompt

TIMEOUT_SECONDS = 900
READS_FILES = True  # `codex exec` opens and edits files itself

# The roles whose prompts say they produce a report rather than a modification
# (see prompts/reviewer.md, prompts/security.md, prompts/decomposer.md) are
# held to that by the sandbox, not merely asked to comply — the same reasoning
# as claude_code.ROLE_ALLOWED_TOOLS, expressed in the vocabulary this CLI has.
ROLE_SANDBOX = {
    "reviewer": "read-only",
    "security": "read-only",
    "decomposer": "read-only",
}
DEFAULT_SANDBOX = "workspace-write"


def _sandbox(agent: str) -> str:
    return ROLE_SANDBOX.get(agent, DEFAULT_SANDBOX)


def _build_prompt(task: AgentTask) -> str:
    """Role prompt, request, then the selected context.

    The role prompt is prepended rather than passed separately because
    `codex exec` has no system-prompt flag. It therefore carries no more
    authority than the rest of the message — worth knowing when comparing a
    role's behaviour across the two providers.
    """
    parts = [p for p in (load_role_prompt(task.engine_root, task.agent), task.description) if p]
    if task.context_render:
        parts.append(task.context_render)
    return "\n\n".join(parts)


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _parse_usage(usage: dict | None, model: str = "") -> TokenUsage | None:
    """Converts codex's usage into the platform convention.

    Codex reports the OpenAI shape, where `cached_input_tokens` is a **subset
    of** `input_tokens`. The platform (providers.base.TokenUsage) wants them
    disjoint, so that input + cache_read + cache_creation is the prompt size.
    Passing these through unchanged would double-count the cached portion:
    a real call reporting input 13,994 / cached 11,008 would be recorded as
    25,002 tokens for a 13,994-token prompt.

    `reasoning_output_tokens` is likewise a subset of `output_tokens` and is
    deliberately not added.
    """
    if not isinstance(usage, dict):
        return None

    total_input = _as_int(usage.get("input_tokens"))
    cached = _as_int(usage.get("cached_input_tokens"))
    return TokenUsage(
        model=model,
        input_tokens=max(total_input - cached, 0),
        output_tokens=_as_int(usage.get("output_tokens")),
        cache_read_tokens=cached,
        cache_creation_tokens=_as_int(usage.get("cache_write_input_tokens")),
        # No cost: a ChatGPT subscription prices nothing per call. None is
        # "unknown", not "free" — see TokenUsage.cost_usd.
    )


def _parse_events(stdout: str) -> tuple[str, dict | None]:
    """Reads the JSONL event stream, returning (last agent message, usage).

    Unparseable lines are skipped rather than failing the run: during real
    work the stream carries item types beyond the ones seen in a trivial
    probe, and a shape we don't recognize is not a reason to discard a call
    that already happened.
    """
    message = ""
    usage: dict | None = None

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        if event.get("type") == "turn.completed":
            reported = event.get("usage")
            if isinstance(reported, dict):
                usage = reported
        elif event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                message = item.get("text") or message

    return message, usage


def _check_auth() -> str | None:
    """Cheap preflight, mirroring claude_code._check_auth: fail in ~100ms
    rather than after building the full command and shipping the context."""
    try:
        proc = subprocess.run(
            ["codex", "login", "status"],
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return (
            "`codex` binary not found in PATH. Install the Codex CLI and run "
            "`codex login` before retrying."
        )
    except subprocess.TimeoutExpired:
        return "codex CLI: `codex login status` timed out."

    if proc.returncode != 0:
        return "Not logged in to Codex. Run `codex login` before retrying."
    return None


def run(task: AgentTask) -> ProviderResult:
    auth_error = _check_auth()
    if auth_error:
        return ProviderResult(success=False, summary=auth_error)

    # --output-last-message is more robust than scanning the event stream for
    # the final message, so it's the primary source; event parsing is the
    # fallback when the file is missing or empty.
    with tempfile.TemporaryDirectory() as tmp:
        last_message_path = Path(tmp) / "last_message.txt"
        cmd = [
            "codex",
            "exec",
            "--json",
        ]
        if task.model:
            cmd.extend(["--model", task.model])
        if task.reasoning_effort:
            cmd.extend(["-c", f"model_reasoning_effort={json.dumps(task.reasoning_effort)}"])
        cmd.extend([
            "--sandbox",
            _sandbox(task.agent),
            "--cd",
            str(task.repo_root),
            "--skip-git-repo-check",
            "--output-last-message",
            str(last_message_path),
            _build_prompt(task),
        ])

        try:
            proc = subprocess.run(
                cmd,
                cwd=task.repo_root,
                capture_output=True,
                text=True,
                timeout=(min(TIMEOUT_SECONDS, task.timeout_seconds) if task.timeout_seconds else TIMEOUT_SECONDS),
                # Without this codex blocks waiting on stdin ("Reading
                # additional input from stdin..."), which in a worker thread
                # is an invisible hang rather than an error.
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return ProviderResult(
                success=False,
                summary=(
                    "`codex` binary not found in PATH. Install the Codex CLI and run "
                    "`codex login` before retrying."
                ),
            )
        except subprocess.TimeoutExpired as exc:
            return ProviderResult(success=False, summary=f"codex CLI: timed out after {exc.timeout}s")

        message, usage = _parse_events(proc.stdout)
        if last_message_path.is_file():
            message = last_message_path.read_text(encoding="utf-8").strip() or message

    # A failed call still consumed quota — attach whatever usage was reported
    # before the failure rather than recording the run as free.
    if proc.returncode != 0:
        return ProviderResult(
            success=False,
            summary=f"codex CLI failed (code {proc.returncode}): {proc.stderr.strip() or message}",
            raw=proc.stdout,
            usage=_parse_usage(usage, task.model or ""),
        )

    if usage is None and not message:
        return ProviderResult(
            success=False,
            summary="codex CLI produced no agent message and no usage — see `raw`.",
            raw=proc.stdout,
        )

    return ProviderResult(success=True, summary=message, raw=proc.stdout, usage=_parse_usage(usage, task.model or ""))
