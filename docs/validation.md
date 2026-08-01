# Validation and sandboxing

## Validation pipeline

Validation is a set of mechanical gates around model-generated changes:

```text
stage execution
  -> tracked, untracked, and ignored-write inventory
  -> role path contract
  -> commit and merge
  -> target tests in disposable worktree
  -> read-only security/reviewer stages
  -> bounded correction when eligible
  -> final report
```

A provider saying that tests pass is not evidence. The platform uses process exit status, changed paths, Git state, and structured review output.

## Frozen target policy

The target repository declares validation in `.ai-platform.yml`. The effective policy is read from the run's base revision before agents execute. An agent cannot weaken the current run by editing the file in its branch.

```yaml
test_command: [uv, run, pytest, -q]
test_timeout: 120
test_sandbox: true
allowed_ephemeral_writes:
  - ".pytest_cache/**"
  - "**/__pycache__/**"
```

Prefer command arrays because they avoid shell parsing ambiguity. A missing policy or command produces an explicit **skipped** result, not a pass. Malformed commands should be treated as configuration errors; silent fallback to skipped is a known gap to eliminate.

## Disposable validation worktree

Target tests execute in a detached temporary worktree created from the integrated delivery revision. Test-generated tracked, untracked, and ignored files cannot pollute the integration worktree. The validation checkout is removed after inventory and result capture unless it must be retained for diagnosis.

## Bubblewrap sandbox

When `test_sandbox: true` and Bubblewrap is available on Linux, the runner:

- disables network access;
- mounts the host filesystem read-only;
- grants write access only to the validation worktree and declared cache paths;
- applies the configured timeout.

If Bubblewrap is unavailable, validation currently runs unsandboxed with a visible warning. That fallback is acceptable for a trusted local developer but not for unattended remote execution. Remote admission should fail closed unless an equivalent sandbox is available.

## Ephemeral writes

Tools legitimately create ignored caches. These writes must be named in `allowed_ephemeral_writes`. Patterns are matched against repository-relative paths.

Examples include `.pytest_cache/**`, `**/__pycache__/**`, coverage data, or a framework-specific build cache. Do not allow broad patterns such as `**/*`: that defeats detection of unexpected side effects.

Unknown ignored writes are contract violations. Allowed ephemeral files remain disposable and are never committed as product changes.

## Review and correction

Reviewer and security roles execute with provider-level read-only controls and are checked for filesystem changes. A final test or review failure may invoke the corrector. Correction is limited by `max_correction_attempts` and is followed by revalidation. Failed upstream DAG stages are not eligible for generic correction because their missing artifacts make repair semantics ambiguous.

## Exit interpretation

| Result | Meaning |
|---|---|
| passed | Configured command ran and exited successfully |
| failed | Command ran and failed, timed out, or violated write policy |
| skipped | No runnable command was configured |
| needs attention | Merge conflict, retained artifact, or unsafe lifecycle failure |
| corrected | Initial eligible failure was repaired and revalidated |

Tests cannot prove the full semantic correctness of a change. Human inspection of the branch and preview remains the final delivery gate.
