# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a GitOps-managed home Kubernetes cluster using FluxCD, Talos Linux, and SOPS for secret encryption.

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
- `*.sops.yaml` - Encrypted secrets

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

- Encrypted files use `.sops.yaml` suffix
- Edit with `sops <file>` CLI
- Never commit plaintext secrets
- Reference secrets via `valuesFrom` in HelmReleases

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
