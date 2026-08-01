# Model and effort routing policy

## Decision

Keep explicit model and effort profiles in `config/presets/profiles/<name>.yaml`. Do not delegate model selection to an agent.

The agent understands the task content, but the engine owns cost, availability, permissions, auditability, and provider compatibility. The useful compromise is bounded autonomy: the decomposer chooses only a complexity class, and deterministic configuration maps that class to ordered Claude/Codex profiles.

## Routing sequence

For each role:

1. select the base `profiles` list for `complex`, or the override in `profiles_by_complexity`;
2. evaluate profiles in declared order;
3. skip a profile when its provider quota gate or exact-profile failure gate is active;
4. run the first profile that clears the gates;
5. if none clears, run the first profile and report that all candidates were gated.

The second profile is a fallback, not a second opinion. No consensus call is made.

## Complexity classes

- **routine**: localized, well-understood, low-risk work with narrow impact;
- **complex**: normal cross-file engineering work or uncertainty requiring substantial reasoning;
- **critical**: architecture, security, broad migrations, irreversible choices, or high blast radius.

One class currently applies to the whole run. This is deliberately conservative and easy to audit. Per-stage complexity may be added later if telemetry demonstrates a clear benefit.

## Policy matrix

Each cell lists the preferred profile, followed by its fallback.

| Role | Routine | Complex | Critical |
|---|---|---|---|
| decomposer | Codex Terra / low; Claude Sonnet / medium | same | same |
| architect | Codex Terra / medium; Claude Sonnet / high | Codex Sol / high; Claude Sonnet / high | Codex Sol / xhigh; Claude Opus / high |
| backend | Codex Terra / medium; Claude Sonnet / medium | Claude Sonnet / high; Codex Terra / high | Codex Sol / high; Claude Opus / xhigh |
| frontend | Codex Terra / medium; Claude Sonnet / medium | Codex Sol / high; Claude Sonnet / high | Codex Sol / xhigh; Claude Opus / xhigh |
| reviewer | Codex Terra / medium; Claude Sonnet / high | Codex Sol / high; Claude Opus / high | Codex Sol / xhigh; Claude Opus / xhigh |
| security | Codex Sol / medium; Claude Sonnet / high | Codex Sol / high; Claude Sonnet / high | Codex Sol / xhigh; Claude Opus / high |
| tests | Codex Terra / low; Claude Sonnet / medium | Codex Terra / medium; Claude Sonnet / medium | Codex Sol / high; Claude Opus / high |
| documentation | Codex Terra / low; Claude Sonnet / low | Codex Terra / low; Claude Sonnet / medium | Codex Sol / medium; Claude Sonnet / high |
| corrector | Codex Terra / medium; Claude Sonnet / medium | Codex Terra / high; Claude Sonnet / high | Codex Sol / high; Claude Opus / xhigh |

Full model identifiers are kept in `config/presets/profiles/<name>.yaml`.

## Rationale

The shipped policy is calibrated for a Pro subscription: Codex handles architecture and security by default, while Claude remains an independent fallback. Critical work still escalates to Codex Sol at `xhigh` and keeps Claude Opus at `high` in reserve, without selecting premium orchestration automatically. Review uses strong independent reasoning but remains read-only. Backend and frontend default to capable implementation profiles; routine work favors balanced models. Tests and documentation usually benefit more from clear contracts and context than maximal reasoning, so their routine defaults are intentionally lighter. Correction escalates with the original run's complexity.

The decomposer stays economical. Its output space is deliberately small and parser-validated, so spending an architecture-grade profile on classification would usually be wasteful.

## Effort semantics

The YAML field is provider-neutral `effort`.

- Codex supports `minimal`, `low`, `medium`, `high`, and `xhigh`.
- Claude Code supports `low`, `medium`, `high`, `xhigh`, `max`, and `ultracode` at the adapter boundary.

Adapters translate the value to the provider CLI. Invalid provider/effort combinations fail configuration validation before execution.

`ultracode` is a Claude Code orchestration mode, not merely another numeric effort value. The shipped Pro policy never selects it automatically; reserve it for an explicit Max-oriented policy or a deliberate manual override. Current Claude Code documentation requires a recent CLI for this mode and for Claude Opus 5; verify versions during deployment.

## Version and naming policy

Model identifiers change over time. Update them deliberately in one policy change, with:

1. official provider documentation checked on the day of the update;
2. router and adapter tests updated;
3. one dry route for every role and complexity;
4. a real-provider smoke test where credentials permit;
5. migration notes if a removed identifier affects historical telemetry.

References:

- [OpenAI model guidance](https://developers.openai.com/api/docs/guides/model-guidance)
- [Claude Code model configuration](https://code.claude.com/docs/en/model-config)
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)
