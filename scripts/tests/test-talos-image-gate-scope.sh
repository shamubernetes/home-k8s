#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

[[ $(printf '%s\n' 'kubernetes/apps/services/vector/app/helmrelease.yaml' | scripts/talos-image-gate-scope) == applicable ]]
[[ $(printf '%s\n' \
  'kubernetes/apps/observability/loki/app/helmrelease.yaml' \
  'scripts/flate-render-baseline.tsv' |
  scripts/talos-image-gate-scope) == applicable ]]
[[ $(printf '%s\n' \
  'kubernetes/apps/rook-ceph/rook-ceph/app/helmrelease.yaml' \
  'scripts/helm-image-pin-baseline.txt' |
  scripts/talos-image-gate-scope) == applicable ]]
[[ $(printf '%s\n' '.github/workflows/image-gate.yaml' 'scripts/talos-image-gate-scope' | scripts/talos-image-gate-scope) == not-applicable ]]
[[ $(printf '%s\n' 'kubernetes/apps/actions-runner-system/ghar-scale-set/arc-talos/helmrelease.yaml' | scripts/talos-image-gate-scope) == not-applicable ]]
[[ $(printf '%s\n' 'README.md' | scripts/talos-image-gate-scope) == not-applicable ]]

if printf '%s\n' \
  'kubernetes/apps/services/vector/app/helmrelease.yaml' \
  '.github/workflows/image-gate.yaml' |
    scripts/talos-image-gate-scope >/dev/null 2>&1; then
  echo 'mixed application and privileged-boundary changes were accepted' >&2
  exit 1
fi

printf 'ok: Talos image gate scope fails closed for mixed changes\n'
