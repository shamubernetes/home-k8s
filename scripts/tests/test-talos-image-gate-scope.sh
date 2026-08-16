#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

[[ $(printf '%s\n' 'kubernetes/apps/services/n8n/n8n/app/helmrelease.yaml' | scripts/talos-image-gate-scope) == applicable ]]
[[ $(printf '%s\n' \
  'kubernetes/apps/observability/victoria-logs/app/helmrelease.yaml' \
  'scripts/flate-render-baseline.tsv' |
  scripts/talos-image-gate-scope) == applicable ]]
[[ $(printf '%s\n' \
  'kubernetes/apps/observability/victoria-logs/app/helmrelease.yaml' \
  'scripts/helm-image-validation-skips.txt' |
  scripts/talos-image-gate-scope) == applicable ]]
[[ $(printf '%s\n' \
  'kubernetes/apps/rook-ceph/rook-ceph/app/helmrelease.yaml' \
  'scripts/helm-image-pin-baseline.txt' |
  scripts/talos-image-gate-scope) == applicable ]]
[[ $(printf '%s\n' \
  'kubernetes/apps/kube-system/cilium/app/ocirepository.yaml' \
  'talos/helmfile.yaml' |
  scripts/talos-image-gate-scope) == applicable ]]
[[ $(printf '%s\n' '.github/workflows/image-gate.yaml' 'scripts/talos-image-gate-scope' | scripts/talos-image-gate-scope) == not-applicable ]]
[[ $(printf '%s\n' 'scripts/helm-image-validation-skips.txt' | scripts/talos-image-gate-scope) == not-applicable ]]
[[ $(printf '%s\n' 'kubernetes/apps/actions-runner-system/ghar-scale-set/arc-talos/helmrelease.yaml' | scripts/talos-image-gate-scope) == not-applicable ]]
[[ $(printf '%s\n' 'talos/talconfig.yaml' | scripts/talos-image-gate-scope) == not-applicable ]]
[[ $(printf '%s\n' 'talos/patches/worker/runtime.yaml' | scripts/talos-image-gate-scope) == not-applicable ]]
[[ $(printf '%s\n' 'kubernetes/apps/kyverno/kyverno/policies/talos-runner-boundary.yaml' | scripts/talos-image-gate-scope) == not-applicable ]]
[[ $(printf '%s\n' 'README.md' | scripts/talos-image-gate-scope) == not-applicable ]]

[[ $(printf '%s\n' \
  'kubernetes/apps/system-upgrade/tuppr/upgrades/talosupgrade.yaml' \
  'talos/talconfig.yaml' |
  scripts/talos-image-gate-scope) == applicable ]]
[[ $(printf '%s\n' \
  'kubernetes/apps/system-upgrade/tuppr/upgrades/talosupgrade.yaml' \
  'kubernetes/apps/services/n8n/n8n/app/helmrelease.yaml' \
  'talos/talconfig.yaml' |
  scripts/talos-image-gate-scope) == applicable ]]
[[ $(printf '%s\n' \
  'kubernetes/apps/system-upgrade/tuppr/upgrades/talosupgrade.yaml' \
  'talos/patches/worker/runtime.yaml' |
  scripts/talos-image-gate-scope) == applicable ]]
[[ $(printf '%s\n' \
  'kubernetes/apps/services/n8n/n8n/app/helmrelease.yaml' \
  'talos/talconfig.yaml' |
  scripts/talos-image-gate-scope) == applicable ]]
[[ $(printf '%s\n' \
  'kubernetes/apps/services/n8n/n8n/app/helmrelease.yaml' \
  '.github/workflows/image-gate.yaml' |
  scripts/talos-image-gate-scope) == applicable ]]

printf 'ok: Talos image gate scope allows mixed application changes\n'
