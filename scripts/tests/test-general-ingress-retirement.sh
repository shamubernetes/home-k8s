#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

[[ ! -d kubernetes/apps/network/ingress-nginx ]] || fail 'legacy ingress-nginx app directory still exists'

while IFS= read -r file; do
  if yq ea -N '[select(.apiVersion == "networking.k8s.io/v1" and .kind == "Ingress")] | length' "$file" | grep -qx '1'; then
    printf '%s\n' "$file"
    fail 'Kubernetes Ingress manifests are forbidden after Envoy cutover'
  fi
done < <(find kubernetes -type f \( -name '*.yaml' -o -name '*.yml' \) -print)

if grep -R -n -E --include='*.yaml' --include='*.yml' 'nginx\.ingress\.kubernetes\.io|kubernetes\.io/ingress\.class|ingress-nginx-(internal|external)' kubernetes; then
  fail 'legacy ingress-nginx annotations or controller dependencies remain'
fi

if grep -R -n 'ingress-nginx-external-controller' kubernetes/apps/network/cloudflared; then
  fail 'Cloudflared still points at ingress-nginx'
fi

if ! grep -R -q 'path: ./kubernetes/apps/network/envoy-gateway/certificates' kubernetes/apps/network/envoy-gateway; then
  fail 'wildcard certificate ownership is not under Envoy Gateway'
fi

printf 'ok: general Ingress and ingress-nginx retirement contract\n'
