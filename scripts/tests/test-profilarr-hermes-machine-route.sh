#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

app=kubernetes/apps/arrs/profilarr/app
route="$app/httproute-envoy-api-hermes.yaml"
policy="$app/securitypolicy-hermes-machine.yaml"

if [[ ! -e $route && ! -e $policy ]]; then
  printf 'ok: Profilarr Hermes machine route contract (full-host-only)\n'
  exit 0
fi

if [[ ! -f $route || ! -f $policy ]]; then
  printf 'error: Profilarr Hermes machine route and authorization policy must appear together\n' >&2
  exit 1
fi

# HOME_DOMAIN is a literal Flux substitution placeholder in the manifest.
# shellcheck disable=SC2016
yq -e '
  .apiVersion == "gateway.networking.k8s.io/v1" and
  .kind == "HTTPRoute" and
  .metadata.name == "profilarr-envoy-api-hermes" and
  .metadata.namespace == "arrs" and
  .spec.hostnames == ["profilarr.${HOME_DOMAIN}"] and
  (.spec.parentRefs | length) == 1 and
  .spec.parentRefs[0].name == "envoy-internal" and
  .spec.parentRefs[0].namespace == "network" and
  .spec.parentRefs[0].sectionName == "https" and
  (.spec.rules | length) == 1 and
  (.spec.rules[0].matches | length) == 1 and
  .spec.rules[0].matches[0].path.type == "PathPrefix" and
  .spec.rules[0].matches[0].path.value == "/api" and
  (.spec.rules[0].matches[0].headers | length) == 1 and
  .spec.rules[0].matches[0].headers[0].name == "X-Api-Key" and
  .spec.rules[0].matches[0].headers[0].type == "RegularExpression" and
  .spec.rules[0].matches[0].headers[0].value == ".+" and
  (.spec.rules[0].backendRefs | length) == 1 and
  .spec.rules[0].backendRefs[0].name == "profilarr" and
  .spec.rules[0].backendRefs[0].port == 6868 and
  ([.spec.rules[0].filters[] | select(.type == "RequestHeaderModifier") | .requestHeaderModifier.remove[]] | sort) ==
    (["X-Auth-Request-Email", "X-Auth-Request-Groups", "X-Auth-Request-User"] | sort)
' "$route" >/dev/null

yq -e '
  .apiVersion == "gateway.envoyproxy.io/v1alpha1" and
  .kind == "SecurityPolicy" and
  .metadata.name == "profilarr-hermes-machine" and
  .metadata.namespace == "arrs" and
  (.spec.targetRefs | length) == 1 and
  .spec.targetRefs[0].kind == "HTTPRoute" and
  .spec.targetRefs[0].name == "profilarr-envoy-api-hermes" and
  .spec.authorization.defaultAction == "Deny" and
  (.spec.authorization.rules | length) == 1 and
  .spec.authorization.rules[0].name == "allow-hermes-mac" and
  .spec.authorization.rules[0].action == "Allow" and
  .spec.authorization.rules[0].principal.clientCIDRs == ["10.0.10.95/32"] and
  (.spec.authorization.rules[0].principal.headers == null) and
  (.spec.oidc == null)
' "$policy" >/dev/null

for resource in httproute-envoy-api-hermes.yaml securitypolicy-hermes-machine.yaml; do
  grep -Fqx -- "- ./$resource" "$app/kustomization.yaml" || {
    printf 'error: Profilarr kustomization must include %s\n' "$resource" >&2
    exit 1
  }
done

printf 'ok: Profilarr Hermes machine route contract (header-gated-mac-only)\n'
