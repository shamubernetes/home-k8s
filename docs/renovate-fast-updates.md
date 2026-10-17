# Fast container updates

`renovate-fast` is an opt-in, deployment-owned Renovate CronJob. It runs every minute independently of the regular Mend Renovate server. The regular server retains its existing thirty-minute scheduler and hourly discovery cadence.

## Add an application

Add one rule to `kubernetes/apps/renovate/renovate/app/config/fast-updates.json`:

```json
{
  "description": "Example is managed by the opt-in fast-update lane",
  "matchDatasources": ["docker"],
  "matchFileNames": ["kubernetes/apps/services/example/app/helmrelease.yaml"],
  "matchPackageNames": ["ghcr.io/example/app"],
  "enabled": false
}
```

`enabled: false` is intentional. Regular Renovate imports this preset and excludes that exact image/file pair. The fast worker reads the same preset and enables only its entries. Do not maintain a separate exclusion list or add wildcard entries. Adding a target still follows the normal protected GitOps publication path.

The initial implementation supports one public GHCR image per app-template HelmRelease, with a stable `major.minor.patch@sha256:...` pin, an owning Flux Kustomization and a Deployment. The app must expose the HTTP readiness path and numeric port declared in its values through a Service named after the Helm release. Unsupported layouts fail closed rather than guessing. Other images in the same file remain disabled. No application source or upstream release workflow needs changing.

Run these checks before publication:

```sh
node --test scripts/tests/test-renovate-fast-updates.cjs
scripts/validate-app renovate/renovate
kubectl kustomize kubernetes/apps/renovate/renovate/access
```

## Behavior

- Uses the same pinned Renovate CE image as the regular producer but starts the embedded standalone CLI with a clean environment and isolated ephemeral cache.
- Reads current `main` and the allowlist through a short-lived GitHub App token restricted to `shamubernetes/home-k8s`. Only the existing Renovate integration's App ID/key are mounted. Database, registry and server-administration credentials are not copied into this worker.
- Ignores the ordinary repository Renovate configuration, including its release-age and automerge rules. Uses only `helm-values`, exact allowlisted paths and per-image enable rules.
- Checks stable version tags and immutable digests without a release-age delay. Docker release/tag/digest soft and hard caches are disabled for this worker only. Each CronJob starts a fresh process.
- Creates ordinary protected PRs on `fast-update/` branches. Uses a distinct dashboard title and branch-history prefix to avoid managing the regular producer's dashboard or PRs.
- Never requests human reviewers or weakens branch protection. Only a bot-authored PR containing one permitted tag-and-digest replacement can merge. The parsed manifest must otherwise be identical, the configured image must be the one changed, and version downgrades are rejected.
- Requires the exact-head GitHub Actions checks for static analysis, Flate, Talos image availability and Talos validation, plus GitHub's protected merge acceptance.
- Merges at most one ready PR per run under the repository's existing bounded maintenance helper. Rechecks that both maintenance members remain active with at least ten minutes left immediately before merging. Verifies the intended Flux revision (or proven descendant retaining the exact target pin), HelmRelease, Deployment image/readiness and configured HTTP health path before ending maintenance. Transient API/health observation failures are retried within the rollout deadline.
- Does not blindly revert a binary after database migrations. A failed rollout is reported as a failed Job, with a finite maintenance expiry, rather than being represented as successful.

Kubernetes prevents overlapping runs with `concurrencyPolicy: Forbid`. A rollout can therefore delay subsequent checks. Registry visibility, GitHub limits, protected CI and rollout time also affect delivery latency; the minute schedule is a check cadence, not a one-minute deployment guarantee.

The worker can read workload/Flux status but cannot modify workloads. Its only Kubernetes mutation permission is the existing maintenance coordination Lease in `observability`.

## Observe and disable

```sh
kubectl -n renovate get cronjob renovate-fast
kubectl -n renovate get jobs --sort-by=.metadata.creationTimestamp
kubectl -n renovate logs job/<job-name> -c renovate
```

Public Pod logs contain reduced phases and dependency counts, not expanded Renovate configuration or tokens. Full diagnostics are private files under `/tmp/renovate-fast` within that short-lived Pod. Inspect them with a local reducer rather than copying raw credential-bearing output into a transcript.

To pause this lane, set `spec.suspend: true` on `fast-update-cronjob.yaml` through GitOps. To return one app to the regular cadence, remove its allowlist rule through GitOps. Do not alter the regular producer schedule.
