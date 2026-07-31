# Configuration

## Agent profiles

`config/agents.yaml` is the source of truth for role-to-profile mapping.

```yaml
architect:
  profiles:
    - provider: claude_code
      model: claude-opus-5
      effort: xhigh
    - provider: codex_cli
      model: gpt-5.6-sol
      effort: high
  profiles_by_complexity:
    routine:
      - provider: claude_code
        model: claude-sonnet-5
        effort: high
    critical:
      - provider: claude_code
        model: claude-opus-5
        effort: ultracode
```

Rules:

- `profiles` is required and is the `complex` policy;
- every profile requires `provider`; `model` and `effort` are explicit in the shipped policy;
- `profiles_by_complexity` accepts only `routine`, `complex`, and `critical`;
- an absent override falls back to `profiles`;
- `effort` must be valid for the selected provider;
- declaring both `effort` and legacy `reasoning_effort` is rejected.

The loader still accepts older `provider`, `providers`, and `reasoning_effort` keys to avoid breaking local installations. New configuration must use `profiles` and `effort`.

## Routing gates

`config/routing.yaml` controls measurable routing gates such as quota pressure, recent profile failures, minimum sample size, and evaluation windows. Gates modify availability, never the semantic ordering chosen in `agents.yaml`.

Quota limits are declared per provider in `config/quota.yaml`. They are estimates based on recorded usage because subscription CLIs do not expose an authoritative remaining balance.

## Workflow

`config/workflow.yaml` defines:

- the fixed task DAG;
- stage dependencies;
- maximum parallelism;
- the bounded correction-attempt count.

The decomposer may select a subset but cannot invent a role absent from this workflow.

## Prompts and contracts

`prompts/<role>.md` contains role instructions. The decomposer prompt has a machine-readable footer:

```text
COMPLEXITY: routine|complex|critical
TASKS: comma-separated workflow task ids
```

The parser accepts only those bounded values. Prompt wording is not a security boundary; filesystem contracts and provider read-only modes enforce write restrictions.

## Target repository

A target can declare its validation command in `.ai-platform.yml`:

```yaml
test_command: [uv, run, pytest, -q]
test_timeout: 120
```

A string command is also accepted. If the file or command is absent, validation is reported as skipped. Target context artifacts are stored in `.ai-platform/` inside that target and should normally be ignored by Git.

## Environment and authentication

Subscription adapters rely on existing CLI sessions:

```bash
codex login
claude auth login
```

API adapters use their provider's environment credentials and separate billing. Do not commit keys, tokens, session files, generated vector indexes, telemetry databases, or provider transcripts.

## Validation behavior

Configuration errors are intentional hard failures. Unknown complexity keys, empty profile lists, invalid profile fields, duplicate effort keys, or unsupported effort/provider pairs must be fixed rather than silently normalized.
