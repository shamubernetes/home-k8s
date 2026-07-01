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
