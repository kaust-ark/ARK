# ADR-0012 — Artifact blobs via an `ArtifactStore` seam (proxy now, presigned later)

- **Status:** Proposed (Phase 3, `feat/byoc-cloud-backend`)
- **Date:** 2026-07-02
- **Deciders:** ARK core
- **Related:** [`../CLOUD_BACKEND_PLAN.md`](../CLOUD_BACKEND_PLAN.md) §Phase 3; [ADR-0001](0001-byoc-thin-control-plane.md); [ADR-0003](0003-http-v1-control-plane-boundary.md); [ADR-0013](0013-state-db-projection.md)

## Context

The dashboard serves a project's binary artifacts — the compiled PDF, an uploaded
PDF, figures, and the "download everything" ZIP — by **reading the orchestrator's
working directory off local disk**: `GET /api/projects/{id}/pdf`
(`website/dashboard/routes.py:3024`, `FileResponse` of `paper/main.pdf`),
`/uploaded-pdf` (`:3040`), and the ZIP (`:3055`). This only works because the remote
orchestrator path **rsyncs its whole working dir back** to the control plane's local
FS every poll and at teardown (`ark/compute/cloud/orchestrator.py`
`poll_orchestrator` ~398–401, `teardown` ~440–447). That rsync bridge is the last
shared-filesystem assumption in the BYOC migration, and it does not survive a control
plane serving many remote orchestrators over HTTP ([ADR-0003](0003-http-v1-control-plane-boundary.md)).

Phase 1 anticipated this and left a stubbed seam: `POST /v1/projects/{id}/artifacts`
returns `{"ok": True}` and stores nothing (`website/dashboard/api.py:213`), and
`ControlPlaneClient.register_artifact(**ref)` is defined but never called
(`ark/controlplane/base.py:118`). There is **no object-storage code anywhere** in the
repo today, and no `ark/artifacts/` package.

Constraints we must not break: on a shared HPC/SLURM mount, artifact serving must be
**behavior-identical** to today; single-node local dev must keep working with **no new
infrastructure** and object storage **defaulted off**; and under BYOC we prefer the
**user's own bucket**, not a bucket we operate.

## Decision

We will introduce an **`ArtifactStore` seam** (`ark/artifacts/`, new) that the
orchestrator writes through and the dashboard resolves through, so no component reads
another's disk.

- **Small interface, three methods.**
  ```python
  class ArtifactStore(ABC):
      def put(self, key: str, stream: BinaryIO, *, content_type: str = "") -> ArtifactRef: ...
      def open(self, ref: ArtifactRef) -> BinaryIO: ...
      def url(self, ref: ArtifactRef, *, expires: int = 3600) -> str | None: ...
  ```
  `url()` returning `None` means "the caller must proxy the bytes."
- **Two implementations.** `LocalArtifactStore` (filesystem, rooted at the project
  dir; `url()` always `None`) and `ObjectArtifactStore` (S3/GCS/Azure Blob; `put`/`open`
  via the provider SDK). **`ObjectArtifactStore.url()` returns `None` for now**, so
  every backend proxies; presigned URLs are a later drop-in (see below).
- **Orchestrator pushes eagerly.** Right after each PDF/figure is produced, the
  orchestrator calls `store.put(...)` then registers the reference via the
  now-real `POST /v1/projects/{id}/artifacts`, backed by a new `Artifact` DB model
  (`project_id`, `kind`, `store_type`, `key`, `content_type`, `size`, `sha256`).
  Eager (not batch-at-teardown) so the dashboard sees a PDF the moment it compiles and
  a crashed VM still leaves its artifacts behind.
- **Dashboard resolves through the store, presigned-ready.** The PDF/figure/ZIP routes
  look up the latest `Artifact`, then:
  ```python
  signed = store.url(ref)
  return RedirectResponse(signed) if signed else StreamingResponse(store.open(ref), ...)
  ```
  Local → `None` → proxy from local disk = **today's behavior**. Object → proxy now →
  redirect later. The presigned upgrade is *only* making `ObjectArtifactStore.url()`
  return a signed URL — no route change.
- **Local goes through the same seam.** The local/SLURM path uses `LocalArtifactStore`,
  not a disk-read shortcut, so CI and dev exercise the abstraction and remote can't
  break in a way local tests miss.
- **Dedicated, orthogonal config.** A new `artifact_store` block (`type: local` by
  default; `s3|gcs|azure` with `bucket`/`prefix`), validated in `validate_config()`
  (`ark/compute/__init__.py`), independent of the launcher × experiment-backend matrix.
  Credentials default to reusing the configured cloud compute backend's if unset.
- **Delete the rsync bridge** once blobs (this ADR) and state ([ADR-0013](0013-state-db-projection.md))
  flow through the API: remove the `sync_from_backend` calls in `orchestrator.py`
  `poll_orchestrator`/`teardown`.

## Consequences

- The dashboard can render a project with **no shared filesystem** between orchestrator
  and control plane — the Phase 3 acceptance bar — while SLURM/local on a shared mount
  keep byte-for-byte today's behavior via `LocalArtifactStore`.
- New surface: an `ark/artifacts/` package, an `Artifact` model + Alembic revision, and
  provider SDK dependencies for `ObjectArtifactStore` (add to the `webapp`/job extras
  behind the `object`/cloud extras).
- Under BYOC the **control plane needs read access** to the user's bucket to proxy (and,
  later, to sign presigned URLs). This is a deployment/credential requirement to
  document, not an architectural change.
- Proxying routes every artifact byte through the control plane; acceptable at current
  scale and the reason presigned URLs are a planned, low-cost follow-up rather than a
  now-requirement.

## Alternatives considered

- **Presigned URLs first.** Rejected for *now*: it imposes CORS setup on the user's
  bucket (BYOC onboarding friction), needs three provider-specific signing paths
  upfront, and has no natural analogue for `LocalArtifactStore` (forcing a fallback
  anyway). The `url()`-returns-`None` design makes it a clean later addition.
- **Shortcut the local path (keep reading disk for `Local`).** Rejected: two
  divergent dashboard code paths that drift, and CI would never exercise the store —
  remote would break in ways local tests can't catch.
- **Reuse the experiment cloud backend's bucket/credentials for artifacts.** Rejected:
  it couples *where artifacts live* to *how experiments run*, breaking `slurm`-
  experiments-plus-object-artifacts and `local`-experiments-plus-remote-artifacts. A
  dedicated `artifact_store` block keeps the axes orthogonal; creds may still default to
  the cloud backend's for convenience.
- **Keep the rsync/pull model.** Rejected: it *is* the shared-FS coupling this phase
  removes, and it cannot serve many remote orchestrators.
