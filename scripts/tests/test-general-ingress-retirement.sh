#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

[[ ! -d kubernetes/apps/network/ingress-nginx ]] || fail 'legacy ingress-nginx app directory still exists'

if rg -n --glob '*.yaml' --glob '*.yml' '^kind:[[:space:]]*Ingress$' kubernetes; then
  fail 'Kubernetes Ingress manifests are forbidden after Envoy cutover'
fi

if rg -n --glob '*.yaml' --glob '*.yml' 'ingress-nginx-(internal|external)|ingress-nginx-external-controller|ingress-nginx-internal-controller' kubernetes; then
  fail 'legacy ingress-nginx controller references remain'
fi

if rg -n --glob '*.yaml' --glob '*.yml' 'nginx\.ingress\.kubernetes\.io/' kubernetes; then
  fail 'NGINX-specific Ingress annotations remain'
fi

rg -q 'path: ./kubernetes/apps/network/envoy-gateway/certificates' kubernetes/apps/network/envoy-gateway/ks.yaml \
  || fail 'wildcard certificate ownership was not preserved under Envoy Gateway'

printf 'ok: general Ingress and ingress-nginx retirement contract\n'
