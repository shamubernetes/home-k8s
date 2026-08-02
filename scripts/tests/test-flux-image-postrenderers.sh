#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
mkdir -p "$tmpdir/checkout/kubernetes/apps/test/app"
cat > "$tmpdir/checkout/kubernetes/apps/test/app/helmrelease.yaml" <<'YAML'
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: test
spec:
  postRenderers:
  - kustomize:
      images:
      - name: ghcr.io/example/combined
        newName: ghcr.io/example/combined
        newTag: v2@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
      - name: registry.k8s.io/example/separate
        newName: registry.k8s.io/example/separate
        newTag: v3
        digest: sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
YAML
cat > "$tmpdir/inventory.json" <<'JSON'
[
  "ghcr.io/example/combined:v2",
  "ghcr.io/example/combined:v1@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "quay.io/example/unmapped:latest",
  "registry.k8s.io/example/separate:v3"
]
JSON

scripts/apply-flux-image-postrenderers \
  --checkout "$tmpdir/checkout" \
  --inventory "$tmpdir/inventory.json" \
  --output "$tmpdir/output.json"

jq -e '. == [
  "ghcr.io/example/combined:v1@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "ghcr.io/example/combined:v2@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "quay.io/example/unmapped:latest",
  "registry.k8s.io/example/separate:v3@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
]' "$tmpdir/output.json" >/dev/null

cat > "$tmpdir/checkout/kubernetes/apps/test/app/conflict.yaml" <<'YAML'
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: conflict
spec:
  postRenderers:
  - kustomize:
      images:
      - name: ghcr.io/example/combined
        newName: ghcr.io/example/combined
        newTag: v2@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
YAML
if scripts/apply-flux-image-postrenderers \
  --checkout "$tmpdir/checkout" \
  --inventory "$tmpdir/inventory.json" \
  --output "$tmpdir/conflict-output.json" >/dev/null 2>&1; then
  echo 'conflicting post-renderer transforms were accepted' >&2
  exit 1
fi

printf '%s\n' '{"not":"an array"}' > "$tmpdir/invalid.json"
if scripts/apply-flux-image-postrenderers \
  --checkout "$tmpdir/checkout" \
  --inventory "$tmpdir/invalid.json" \
  --output "$tmpdir/invalid-output.json" >/dev/null 2>&1; then
  echo 'invalid Flate inventory was accepted' >&2
  exit 1
fi

grep -Fq 'apply-flux-image-postrenderers' scripts/render-flate-image-inventory
printf 'ok: exact Flux image post-renderers normalize Flate inventory fail-closed\n'
