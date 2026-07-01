# AGENTS.md

This file provides guidance to All Agents when working with code in this repository.

## Project Overview

This is a GitOps-managed home Kubernetes cluster using FluxCD and Talos Linux. Runtime application secrets normally come from 1Password via External Secrets (`op-secret-store`). SOPS is still used for Flux cluster variables, Talos/bootstrap secrets, and existing SOPS-managed resources.

## Common Commands

```bash
# List all available tasks
task

# Apply a Flux Kustomization manually
task kubernetes:apply-ks cluster=main path=<category>/<app>/app

# Browse a PVC interactively
task kubernetes:browse-pvc ns=<namespace> claim=<pvc-name>

# Delete failed/evicted pods
task kubernetes:delete-failed-pods

# Sync ExternalSecrets
task kubernetes:sync-secrets ns=<namespace> secret=<name>

# Validate a GitOps app without applying it
task kubernetes:validate-app app=<category>/<app>

# Resolve a mutable image tag to an immutable digest reference
task kubernetes:pin-image image=docker.io/library/nginx:latest

# Check app-template images for missing digest pins
task kubernetes:check-image-pins

# Scaffold a standard app-template app
task kubernetes:new-app category=tools app=example args="--image docker.io/library/nginx:latest --port 8080 --internal"

# Reconcile and wait for a pushed app
task kubernetes:reconcile-app app=tools/searxng

# Bootstrap entire cluster (destructive)
task bootstrap:kubernetes nodes=<node1,node2> disk=/dev/nvme0n1
```

## Architecture

### Directory Structure

- `kubernetes/apps/<category>/<app>/` - Application deployments organized by category (arrs, media, observability, etc.)
- `kubernetes/flux/` - Flux configuration, repositories, and cluster variables
- `kubernetes/templates/` - Reusable templates referenced by app kustomizations
- `kubernetes/bootstrap/` - Initial cluster bootstrap resources
- `talos/` - Talos Linux cluster configuration (`talconfig.yaml`, patches, secrets)
- `.taskfiles/` - Task automation scripts

### Flux GitOps Flow

1. `kubernetes/flux/apps.yaml` is the root Kustomization that applies everything under `kubernetes/apps/`
2. It enables SOPS decryption via `sops-age` secret
3. Variable substitution from: `cluster-settings` (ConfigMap), `cluster-secrets` (Secret), `cluster-ipam` (ConfigMap), `volsync-schedules` (ConfigMap)
4. Child Kustomizations inherit decryption and substitution unless labeled `substitution.flux.home.arpa/disabled: "true"`

### App Layout Pattern

Each app follows: `kubernetes/apps/<category>/<app>/app/`
- `kustomization.yaml` - References resources and templates
- `helmrelease.yaml` - HelmRelease with chart configuration
- `externalsecret.yaml` - Runtime app secrets from 1Password via External Secrets when needed
- `*.sops.yaml` - Only for Flux/Talos/bootstrap secrets or existing areas that intentionally use SOPS directly

### App Category Placement

- `tools` - agent/admin/operator utilities and small cluster helper UIs
- `services` - user-facing internal apps that do not fit a narrower domain
- `network` - ingress, DNS, routing, SMTP, tunnels, network controllers
- `database` - database operators, shared datastores, caches, and database cluster resources
- `observability` - monitoring, alerting, logging, dashboards, status checks
- `media` - media library, playback, download, or metadata apps
- `arrs` - Arr-stack apps and download automation
- `games` - game servers and game-specific routers
- `security` - identity, auth, policy, and security services

## Tooling and Python

- Use the tools declared in `.mise.toml` for this repo: `kubectl`, `sops`, `age`, `task`, `flux2`, `talhelper`, `talosctl`, `krew`, and Renovate.
- Do not treat missing `pyenv` or missing system Python packages such as PyYAML as a repo problem. This repository does not declare a Python runtime or Python dependencies.
- For YAML inspection and transformation, prefer `yq`, `kubectl kustomize`, `helm template`, and the scripts under `scripts/`.
- If a helper script uses Python, keep it Python standard-library only unless a Python environment and dependency file are added deliberately.
- Do not install Python packages globally/Homebrew just to inspect YAML. If a one-off Python dependency is unavoidable, use an isolated temporary environment and do not make it part of the repo workflow without adding explicit project config.

## YAML Conventions

### Schema Headers (required first line)
```yaml
# HelmRelease
# yaml-language-server: $schema=https://k8s-schemas.pages.dev/helm.toolkit.fluxcd.io/helmrelease_v2.json

# Flux Kustomization
# yaml-language-server: $schema=https://k8s-schemas.pages.dev/kustomize.toolkit.fluxcd.io/kustomization_v1.json

# Plain kustomization.yaml
# yaml-language-server: $schema=https://json.schemastore.org/kustomization
```

### HelmRelease Defaults
- `spec.interval: 10m`
- `spec.install.remediation.retries: 3`
- `spec.upgrade.cleanupOnFail: true`
- `spec.upgrade.remediation.strategy: rollback`, `retries: 3`
- Skip CRD management unless chart requires it

### Style
- 2-space indentation, no tabs
- Top-level key order: `apiVersion`, `kind`, `metadata`, `spec`
- Use YAML anchors for repeated values (`&probes`, `&cephBucket`)
- Variable substitution uses `${VAR_NAME}` syntax (processed by Flux postBuild)
- End files with single newline

## Secrets Management

- Runtime app secrets should use 1Password plus External Secrets by default:
  - Create or update an item in the `Kubernetes` vault.
  - Add an `ExternalSecret` using `secretStoreRef.kind: ClusterSecretStore` and `secretStoreRef.name: op-secret-store`.
  - Target the app secret as `<app>-secret` unless the chart expects a different name.
  - Consume the generated Secret with `envFrom.secretRef`, explicit env refs, or `valuesFrom` as appropriate.
- Use SOPS (`*.sops.yaml`) for Flux cluster variables, Talos/bootstrap secrets, and existing SOPS-managed resources, not as the default for new app runtime secrets.
- Never commit plaintext secrets.

## Shared Data Services

Prefer existing shared data platforms before adding embedded sidecars or app-local singleton databases.

### PostgreSQL

- Use the existing CloudNativePG/Postgres 17 service for Postgres-compatible apps.
- Default app connection host: `postgres17-pooler.database.svc.cluster.local`.
- Direct writer host is available as `postgres17-rw.database.svc.cluster.local` when an app requires direct session behavior that the pooler breaks.
- Initialize app databases/users through GitOps-owned init resources or established app patterns, not manual psql mutations.
- Store app DB credentials in 1Password and surface them with `ExternalSecret` via `op-secret-store`.
- Common env pattern:
  - `DATABASE_URL=postgresql://...@postgres17-pooler.database.svc.cluster.local:5432/<db>`
  - `INIT_POSTGRES_HOST=postgres17-pooler.database.svc.cluster.local`
  - `INIT_POSTGRES_DBNAME`, `INIT_POSTGRES_USER`, `INIT_POSTGRES_PASS`, `INIT_POSTGRES_SUPER_PASS`

### MariaDB/MySQL

- Use the MariaDB Operator in the `database` namespace for MariaDB/MySQL-compatible apps.
- Prefer operator-managed app-specific MariaDB resources through GitOps over embedded database sidecars.
- Use `ceph-block` storage unless the app has a documented reason to use a different storage backend.
- Only use same-pod database sidecars for small apps when no suitable shared/operator path exists, and document why.

### Redis-compatible cache/queue

- Use shared Dragonfly for Redis-compatible needs unless isolation requires otherwise.
- In-cluster URL pattern: `redis://dragonfly.database.svc.cluster.local:6379/<db>`.
- If a workload in another namespace uses Dragonfly and network policy is enforced, update the Dragonfly client NetworkPolicy in GitOps before rollout.

### Object storage

- Prefer existing Rook-Ceph object storage/RGW patterns for S3-compatible needs.
- Keep browser-facing presigned URLs on a hostname the browser can reach, not an in-cluster service DNS name.
- Store S3 credentials in 1Password and expose through `ExternalSecret`.

## Talos Configuration

- Main config: `talos/talconfig.yaml`
- Per-node configs: `talos/clusterconfig/`
- Patches: `talos/patches/global/` and `talos/patches/controlplane/`
- Encrypted secrets: `talos/talsecret.sops.yaml`

## App-Template (bjw-s) Pattern

When using `app-template` charts:
- Use `controllers` with per-controller containers, probes (anchor `&probes`), and restrictive `securityContext`
- `defaultPodOptions.securityContext`: `runAsNonRoot`, `runAsUser`, `runAsGroup`, `fsGroup`, `seccompProfile: RuntimeDefault`
- External ingress: `ingressClassName: external` with external-dns
- Internal ingress: `ingressClassName: internal`
- LoadBalancers: Use Cilium annotation `io.cilium/lb-ipam-ips: "${IPAM_IP_*}"`

## New App Validation

Before committing a new or changed app, run:

```bash
scripts/validate-app <category>/<app>
# or
task kubernetes:validate-app app=<category>/<app>
```

This renders the app kustomization, server-dry-runs non-Secret manifests, renders HelmReleases with common Flux substitutions, and server-dry-runs the rendered chart output. Raw Secret manifests are skipped during local dry-run because SOPS material may only be available to Flux.

For public images, pin mutable tags before committing:

```bash
scripts/pin-image docker.io/searxng/searxng:latest
# or
task kubernetes:pin-image image=docker.io/searxng/searxng:latest
```

To scaffold a new standard app-template app:

```bash
scripts/new-app tools example --image docker.io/library/nginx:latest --port 8080 --internal
# or
task kubernetes:new-app category=tools app=example args="--image docker.io/library/nginx:latest --port 8080 --internal"
```

After pushing app changes, reconcile and wait with:

```bash
scripts/reconcile-app tools/searxng
# or
task kubernetes:reconcile-app app=tools/searxng
```
