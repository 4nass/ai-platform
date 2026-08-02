# ADR-010: A project registry, not a path, is the admission boundary

- Status: Accepted
- Date: 2026-08-02

## Context

`--repo /any/path` is the only way to say what a run operates on. That is the right interface for someone standing at their own workstation: they can already `cd` there, and the engine adds nothing by second-guessing a directory its owner named.

It is an unacceptable interface for a request that arrived as chat text. A remote caller supplying a path means an attacker-chosen path, and what the engine then does to it — index it, execute its declared test command, let agents write to it — is not something to do to an arbitrary directory. Issue [#25](https://github.com/4nass/ai-platform/issues/25), and gate 1 of the remote-readiness list in [docs/security.md](../security.md).

## Decision

A caller that is not standing at the machine names a **project id**, never a path. `config/projects.yaml` maps ids to paths and to what may be done to each, and the engine — not the caller — performs that resolution (`core/orchestrator/registry.py`).

- **Paths are canonicalized, then contained.** `Path.resolve()` collapses `..` and follows symlinks, and the result must be under a declared `roots` entry, tested with `Path.is_relative_to` rather than a string prefix. A registry with projects but no roots is refused outright: an allowlist without a boundary is just a list.
- **A path is not an identity.** The declared remote and base branch are verified against the repository actually on disk, so a directory that was replaced or re-cloned from somewhere else is refused rather than silently operated on.
- **Actions are separate grants.** `inspect`, `modify`, `test` and `open_pr` are distinct; a project declaring nothing gets `inspect` alone. `test` is its own grant because executing a target's declared command is arbitrary code execution on this machine, not a consequence of being writable.
- **Resolution happens before anything else.** Admission runs in the CLI, ahead of context indexing and provider selection, so a refusal costs nothing. For queued jobs it runs **again at claim time**, in the worker: a job can execute hours after submission, and an allowlist consulted only at submission is a snapshot taken at the least useful moment.
- **Refusals say nothing.** An unknown id does not report which ids exist or where they point. Probing for valid ids is the first thing an unauthorized caller does.
- **The resolved policy is recorded on the run.** The file can be edited afterwards, and "this run executed the target's tests" means something different depending on whether that was granted at the time.

`--repo` is unchanged and remains the local path. The two are refused together: they name the same thing two ways, and picking one silently would decide a trust question on the caller's behalf.

## Consequences

Anything reaching the engine over a wire can only name what the owner has written down, and that grant is re-checked at the moment it is used rather than the moment it was requested. Withdrawing a project from the registry takes effect for jobs already queued.

A second engine-level config file exists, which [ADR-008](ADR-008-platform-config-and-presets.md) deliberately argued against. The argument there was about one policy surface fragmented across six files; this is a different surface with a different lifecycle (edited when you add a repository, not when you tune behaviour) and a different blast radius (a mistake changes what can be reached at all, not how well runs go). Keeping it separate and small is what makes it auditable at a glance.

`open_pr` is declarable but not implemented ([#33](https://github.com/4nass/ai-platform/issues/33)). Declaring it now means a project can withhold the capability before it exists, rather than being granted it retroactively on the day it ships.

## Alternatives

- **A `projects:` section inside `platform.yaml`:** rejected. It mixes inventory with tuning, so every routine tuning edit touches the file that decides what is reachable.
- **Keeping `--repo` and validating it against roots:** rejected as the remote answer. It still lets the caller choose the path and reduces the engine to checking a prefix; the id indirection is what removes caller-supplied paths entirely.
- **Resolving once at submission and trusting the stored path:** rejected — see the claim-time re-check above.
- **Deriving the allowlist from what is already on disk (e.g. every repo under `~/workspace`):** rejected. Cloning a repository would then grant it access, which inverts who decides.
