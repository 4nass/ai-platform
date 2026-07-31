"""Claude Code CLI provider: Hermes doesn't talk to the model, it drives the CLI.

`claude -p` runs in non-interactive mode, authenticated via the already
active subscription session (`claude auth login`) — no API key. The CLI
edits files itself (Read/Edit/Write); this provider only invokes the
process and relays its summary, per the providers.base.Provider contract.

Flags verified against the Claude Code documentation, and the shape of the
output JSON confirmed empirically (`result`, `is_error`, `subtype`,
`session_id`, `total_cost_usd`) — the binary is present in this sandbox but
not authenticated, which is enough to validate parsing without being able
to test an actual task run.
"""

from __future__ import annotations

import json
import subprocess

from providers.base import AgentTask, ProviderResult, TokenUsage, load_role_prompt

TIMEOUT_SECONDS = 900
READS_FILES = True  # the CLI opens files itself via its Read/Grep/Glob tools
DEFAULT_ALLOWED_TOOLS = "Read,Edit,Write,Bash(uv run pytest*)"
# The reviewer/security roles must never edit files (see prompts/reviewer.md,
# prompts/security.md: their output is a report, not a modification) —
# enforced here at the tool level, not just via the prompt.
# architect/documentation write documents but don't need to run commands.
ROLE_ALLOWED_TOOLS = {
    "reviewer": "Read,Grep,Glob",
    "security": "Read,Grep,Glob",
    "decomposer": "Read,Grep,Glob",
    "architect": "Read,Edit,Write,Grep,Glob",
    "documentation": "Read,Edit,Write,Grep,Glob",
}


def _allowed_tools(agent: str) -> str:
    return ROLE_ALLOWED_TOOLS.get(agent, DEFAULT_ALLOWED_TOOLS)


def _build_prompt(task: AgentTask) -> str:
    """The request plus the selected context, as rendered for this provider.

    This used to re-derive its own listing from `context_paths` and drop
    `context_render` on the floor, which meant the entire selection pipeline
    — chunking, embeddings, vector search, the dependency graph, PageRank —
    reached the model as a flat alphabetical list of filenames. Whatever the
    context layer decided to send is injected here verbatim.
    """
    if not task.context_render:
        return task.description
    return f"{task.description}\n\n{task.context_render}"


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _parse_usage(data: dict | None) -> TokenUsage | None:
    """Maps the CLI's JSON into the shared TokenUsage shape.

    Every field is read defensively: the payload carries usage even on the
    error path (verified against a real 401), but this must never be the
    thing that breaks a run if the CLI's output shape changes.
    """
    if not isinstance(data, dict):
        return None

    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}

    # `modelUsage` is keyed by model name. More than one key means the call
    # spanned models (e.g. a fallback); record them all rather than silently
    # attributing the whole cost to whichever happened to come first.
    model_usage = data.get("modelUsage")
    models = sorted(model_usage) if isinstance(model_usage, dict) else []

    cost = data.get("total_cost_usd")
    duration = data.get("duration_ms")

    return TokenUsage(
        model=", ".join(models),
        input_tokens=_as_int(usage.get("input_tokens")),
        output_tokens=_as_int(usage.get("output_tokens")),
        cache_read_tokens=_as_int(usage.get("cache_read_input_tokens")),
        cache_creation_tokens=_as_int(usage.get("cache_creation_input_tokens")),
        cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
        provider_duration_ms=duration if isinstance(duration, int) and not isinstance(duration, bool) else None,
    )


def _check_auth() -> str | None:
    """Cheap preflight so an unauthenticated session fails in ~100ms instead
    of after building the full task prompt/command."""
    try:
        proc = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return (
            "`claude` binary not found in PATH. Install the Claude Code CLI "
            "and run `claude auth login` before retrying."
        )
    except subprocess.TimeoutExpired:
        return "claude CLI: `claude auth status` timed out."

    try:
        data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        data = {}

    if not data.get("loggedIn"):
        return "Not logged in to Claude Code. Run `claude auth login` before retrying."
    return None


def run(task: AgentTask) -> ProviderResult:
    auth_error = _check_auth()
    if auth_error:
        return ProviderResult(success=False, summary=auth_error)

    system_prompt = load_role_prompt(task.repo_root, task.agent)

    cmd = [
        "claude",
        "-p",
        _build_prompt(task),
        "--add-dir",
        str(task.repo_root),
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        _allowed_tools(task.agent),
        "--output-format",
        "json",
    ]
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]

    try:
        proc = subprocess.run(
            cmd,
            cwd=task.repo_root,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return ProviderResult(
            success=False,
            summary=(
                "`claude` binary not found in PATH. Install the Claude Code CLI "
                "and run `claude auth login` before retrying."
            ),
        )
    except subprocess.TimeoutExpired as exc:
        return ProviderResult(success=False, summary=f"claude CLI: timed out after {exc.timeout}s")

    # Error info (e.g. "Not logged in · Please run /login") arrives in the
    # JSON on stdout, not stderr — try parsing before trusting the exit code alone.
    data: dict | None = None
    if proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout)
            data = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            data = None

    # A failed call still consumed tokens and still cost money — attach usage
    # on the error paths too, or the accounting silently understates spend.
    if proc.returncode != 0:
        if data and data.get("result"):
            return ProviderResult(
                success=False,
                summary=f"claude CLI (code {proc.returncode}): {data['result']}",
                raw=data,
                usage=_parse_usage(data),
            )
        return ProviderResult(
            success=False,
            summary=f"claude CLI failed (code {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}",
            raw=proc.stderr or proc.stdout,
        )

    if data is None:
        return ProviderResult(
            success=False,
            summary="Non-JSON output from the claude CLI — see `raw` for the raw content.",
            raw=proc.stdout,
        )

    if data.get("is_error"):
        return ProviderResult(
            success=False,
            summary=data.get("result", "Unknown error"),
            raw=data,
            usage=_parse_usage(data),
        )

    return ProviderResult(success=True, summary=data.get("result", ""), raw=data, usage=_parse_usage(data))
