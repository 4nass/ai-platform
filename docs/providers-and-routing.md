# Providers and routing

## Provider contract

Every adapter receives a normalized task containing the role, request, context, worktree, complexity, model, and effort. It returns a normalized result with outcome, text, usage, timing, and failure information. The worktree diff—not the provider's prose—is the authoritative code artifact.

## Adapter status

| Provider | Adapter | State | Notes |
|---|---|---|---|
| Claude Code | `claude -p` | Delivered | Uses local authenticated CLI session |
| Codex | `codex exec --json` | Delivered | Uses local authenticated CLI session |
| Anthropic API | Python SDK | Available but not primary | Separate credentials and billing |
| OpenAI API | adapter stub | Not delivered | Must not be selected as functional |
| Local model | none | Planned | [#37](https://github.com/4nass/ai-platform/issues/37) |

CLI authentication must be established before unattended work:

```bash
codex login
claude auth login
```

Preflight and actionable Claude authentication errors are tracked by [#8](https://github.com/4nass/ai-platform/issues/8).

## Explicit semantic policy

`config/agents.yaml` orders profiles for every role. A profile is:

```yaml
provider: codex_cli
model: gpt-5.6-sol
effort: high
```

Roles can override this list for `routine`, `complex`, and `critical` work. Architecture, security, and correction favor stronger models or higher effort for critical work; routine implementation favors efficient profiles. Both Claude and GPT/Codex candidates are declared so failover preserves role intent.

The router does not let an agent choose its own provider. Agent autonomy operates inside a bounded execution profile; model selection is a platform policy because it affects cost, latency, quality, and auditability.

## Selection algorithm

1. Load the role and complexity profile order.
2. Reject temporarily unhealthy candidates using recent profile outcomes.
3. Gate candidates whose locally estimated quota ratio exceeds the configured threshold.
4. Select the first eligible profile.
5. If every profile is gated, select the first declared profile anyway and record a forced-fallback reason so a run does not deadlock.
6. Record provider, model, effort, and routing reason in telemetry.

Current default gates include a quota ratio of 0.85 and a minimum success rate of 0.60 after at least five samples in a 24-hour window. Configuration is authoritative.

Quota values for subscription CLIs are declared estimates, not provider-issued remaining balances. Therefore routing pressure is advisory and cannot enforce a financial ceiling. Hard admission budgets are tracked by [#27](https://github.com/4nass/ai-platform/issues/27).

## Model and effort fidelity

The adapter should report the effective model and effort, not merely the requested values. Claude and Codex expose different effort vocabularies and flags; unsupported combinations are rejected during configuration loading. Remaining effective-model observability for Codex is tracked by [#16](https://github.com/4nass/ai-platform/issues/16).

See [Model and effort routing policy](model-routing-policy.md) for the role-by-role intent.

## Failures and failover

Failures are classified so authentication, quota pressure, malformed output, timeouts, provider crashes, and contract violations do not all poison routing equally. Recent performance can move a profile behind another candidate, but semantic ordering remains owned by policy. Stronger provider failover behavior is tracked by [#31](https://github.com/4nass/ai-platform/issues/31); quality-aware routing by [#32](https://github.com/4nass/ai-platform/issues/32).

Provider transcripts and credentials may contain sensitive data. They must not be committed, and future remote operation requires explicit retention and secrets isolation.
