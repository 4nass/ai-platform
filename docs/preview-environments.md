# Per-run preview environments

Issue #34 is implemented as a provider-neutral lifecycle. The engine never
deploys arbitrary source or exposes a workstation port. It accepts a typed
PreviewDeployPlan and requires a provider to build exactly its commit.

## Contract

Attach PreviewActionHandler to the shared ActionExecutor:

    manager = PreviewManager(
        engine_root,
        provider,
        allowed_hosts=("preview.example.com",),
        credential_provider=project_credentials,
    )
    executor = ActionExecutor(
        engine_root,
        handlers={PREVIEW_DEPLOY: PreviewActionHandler(manager)},
    )

A provider implements:

- deploy(plan, context) -> PreviewDeployment;
- cleanup(preview, context) -> PreviewCleanup.

The deploy response must contain an HTTPS URL under an allowed preview domain,
the exact source commit, an external deployment id, and either provider-side
authentication or capability authentication. Public URLs are rejected.

## Security and reproducibility

The plan pins a 40-character commit SHA, service, environment, configuration
digest, data mode and bounded TTL. The shared action executor applies project
allowlists and exact fingerprint-bound approvals before the provider is called.

For capability authentication, the manager creates a random expiring bearer
token, passes it to the provider in PreviewContext and appends it to the
returned URL. Only its SHA-256 digest is used for capability authorization.
The URL is an expiring artifact and must be retained with the restrictive
permissions of jobs.sqlite; issue #35 defines future centralized retention and
redaction.

Credentials are requested by project id and passed opaquely to the provider.
They are never included in plans, audit payloads, mobile responses or provider
errors. Database-backed projects must select data_mode=ephemeral or
data_mode=readonly; the provider is responsible for enforcing that choice.

## Lifecycle and cleanup

The durable states are requested, deploying, ready, failed, expired,
superseded, cleaning, cleaned and cleanup_failed. Every transition is appended
to preview_events and emits a structured job event when a job id is present.

PreviewManager.reconcile() expires ready/deploying previews whose TTL elapsed
and invokes provider cleanup. A new preview for the same project and run marks
the previous preview superseded and invokes cleanup. Cleanup failures remain
visible and are never silently retried.

## REST/SSE surface

Authenticated clients can read:

- GET /v1/jobs/{job_id}/preview for the full safe preview record;
- GET /v1/jobs/{job_id}/artifacts for an expiring preview URL plus status,
  expiry and immutable commit metadata;
- GET /v1/jobs/{job_id}/events for preview.requested, preview.deploying,
  preview.ready, preview.failed, preview.expired, preview.cleaned and
  preview.cleanup_failed events.

The API authorizes these reads against the job principal. The provider URL is
not a replacement for API authentication: the provider or its edge must
enforce the capability token or its own authentication mode.

## Remaining integration

The engine deliberately does not choose a cloud, CI vendor, DNS provider or
database implementation. A production adapter must build from the committed
delivery branch, configure an isolated hostname, provision project-scoped
secrets, enforce the selected data mode and invoke reconcile from a managed
worker. This is the remaining concrete integration work for #34/#21.
