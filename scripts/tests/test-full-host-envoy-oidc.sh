#!/usr/bin/env bash
# shellcheck disable=SC2016 # Flux placeholders are intentionally literal in repository manifests.
set -euo pipefail
repo_root=$(git rev-parse --show-toplevel); cd "$repo_root"
validate_full_host() {
  local category=$1 app_dir=$2 resource_name=$3 host_name=$4 backend_name=$5 backend_port=$6
  local app="kubernetes/apps/${category}/${app_dir}/app"
  local host="${host_name}.\${HOME_DOMAIN}"
  local oidc_item="${resource_name}-oidc" oidc_secret="${resource_name}-oidc-secret"
  local browser_route="${resource_name}-envoy-browser" dns_resource="${app}/service-envoy-dns.yaml"
  local resources=(externalsecret-oidc.yaml httproute-envoy-browser.yaml securitypolicy-oidc.yaml)
  local present=0 resource
  for resource in "${resources[@]}"; do [[ -f "${app}/${resource}" ]] && ((present+=1)); done
  if ((present==0)); then return 0; fi
  if ((present!=${#resources[@]})); then printf 'partial full-host OIDC contract for %s\n' "$resource_name" >&2; return 1; fi
  for resource in "${resources[@]}"; do grep -Fqx -- "- ./${resource}" "$app/kustomization.yaml"; done
  [[ ! -f "$app/httproute-envoy-api.yaml" ]]
  if grep -Fqx -- '- ./httproute-envoy-api.yaml' "$app/kustomization.yaml"; then
    printf 'unsafe API bypass listed for full-host application %s\n' "$resource_name" >&2; return 1
  fi
  export host backend_name backend_port browser_route resource_name oidc_item oidc_secret
  yq -e '
    .kind == "HTTPRoute" and
    .metadata.name == strenv(browser_route) and
    (.spec.hostnames | length) == 1 and
    .spec.hostnames[0] == strenv(host) and
    (.spec.parentRefs | length) == 1 and
    .spec.parentRefs[0].group == "gateway.networking.k8s.io" and
    .spec.parentRefs[0].kind == "Gateway" and
    .spec.parentRefs[0].name == "envoy-internal" and
    .spec.parentRefs[0].namespace == "network" and
    .spec.parentRefs[0].sectionName == "https" and
    (.spec.rules | length) == 1 and
    (.spec.rules[0].matches | length) == 1 and
    .spec.rules[0].matches[0].path.type == "PathPrefix" and
    .spec.rules[0].matches[0].path.value == "/" and
    (.spec.rules[0].backendRefs | length) == 1 and
    .spec.rules[0].backendRefs[0].group == "" and
    .spec.rules[0].backendRefs[0].kind == "Service" and
    .spec.rules[0].backendRefs[0].name == strenv(backend_name) and
    .spec.rules[0].backendRefs[0].port == (strenv(backend_port) | tonumber) and
    (.spec.rules[0].filters | length) == 1 and
    .spec.rules[0].filters[0].type == "RequestHeaderModifier" and
    (.spec.rules[0].filters[0].requestHeaderModifier.remove | length) == 4 and
    .spec.rules[0].filters[0].requestHeaderModifier.remove[0] == "Authorization" and
    .spec.rules[0].filters[0].requestHeaderModifier.remove[1] == "X-Auth-Request-User" and
    .spec.rules[0].filters[0].requestHeaderModifier.remove[2] == "X-Auth-Request-Email" and
    .spec.rules[0].filters[0].requestHeaderModifier.remove[3] == "X-Auth-Request-Groups"
  ' "$app/httproute-envoy-browser.yaml" >/dev/null
  yq -e '
    .kind == "SecurityPolicy" and .metadata.name == strenv(oidc_item) and
    (.spec.targetRefs | length) == 1 and .spec.targetRefs[0].group == "gateway.networking.k8s.io" and
    .spec.targetRefs[0].kind == "HTTPRoute" and .spec.targetRefs[0].name == strenv(browser_route) and
    .spec.oidc.provider.issuer == "https://sso.${HOME_DOMAIN}" and
    .spec.oidc.clientIDRef.name == strenv(oidc_secret) and .spec.oidc.clientSecret.name == strenv(oidc_secret) and
    .spec.oidc.redirectURL == ("https://" + strenv(host) + "/oauth2/callback") and
    .spec.oidc.logoutPath == "/oauth2/logout" and (.spec.oidc.scopes | length) == 3 and
    .spec.oidc.scopes[0] == "openid" and .spec.oidc.scopes[1] == "profile" and .spec.oidc.scopes[2] == "email" and
    .spec.oidc.forwardAccessToken == false and .spec.oidc.forwardIDToken == null and
    .spec.oidc.passThroughAuthHeader == false and .spec.oidc.cookieConfig.sameSite == "Lax" and
    .spec.oidc.cookieNames.accessToken == (strenv(resource_name) + "-access-token") and
    .spec.oidc.cookieNames.idToken == (strenv(resource_name) + "-id-token")
  ' "$app/securitypolicy-oidc.yaml" >/dev/null
  yq -e '
    .kind == "ExternalSecret" and .metadata.name == strenv(oidc_item) and
    .spec.secretStoreRef.kind == "ClusterSecretStore" and .spec.secretStoreRef.name == "op-secret-store" and
    .spec.target.name == strenv(oidc_secret) and .spec.target.creationPolicy == "Owner" and
    .spec.target.deletionPolicy == "Retain" and (.spec.data | length) == 2 and
    .spec.data[0].secretKey == "client-id" and .spec.data[0].remoteRef.key == strenv(oidc_item) and
    .spec.data[0].remoteRef.property == "client-id" and .spec.data[1].secretKey == "client-secret" and
    .spec.data[1].remoteRef.key == strenv(oidc_item) and .spec.data[1].remoteRef.property == "client-secret"
  ' "$app/externalsecret-oidc.yaml" >/dev/null
  local app_controller api_controller state
  app_controller=$(yq -r '.spec.values.ingress.app.annotations."external-dns.alpha.kubernetes.io/controller" // ""' "$app/helmrelease.yaml")
  api_controller=$(yq -r '.spec.values.ingress.api.annotations."external-dns.alpha.kubernetes.io/controller" // ""' "$app/helmrelease.yaml")
  if [[ -f $dns_resource ]]; then
    [[ $app_controller == ignore ]]
    if yq -e '.spec.values.ingress.api != null' "$app/helmrelease.yaml" >/dev/null; then [[ $api_controller == ignore ]]; fi
    grep -Fqx -- '- ./service-envoy-dns.yaml' "$app/kustomization.yaml"
    yq -e '
      .kind == "Service" and .metadata.name == (strenv(resource_name) + "-envoy-dns") and
      .metadata.annotations."external-dns.alpha.kubernetes.io/hostname" == strenv(host) and
      .metadata.annotations."external-dns.alpha.kubernetes.io/target" == "${IPAM_IP_ENVOY_INTERNAL}" and
      .spec.type == "ExternalName" and .spec.externalName == "envoy-internal.network.svc.cluster.local"
    ' "$dns_resource" >/dev/null
    state=cutover
  else
    [[ -z $app_controller ]]; state=staged
  fi
  yq -e '.spec.values.ingress.app.annotations."nginx.ingress.kubernetes.io/auth-url" == "http://oauth2-proxy.arrs.svc.cluster.local:4180/oauth2/auth" and .spec.values.ingress.app.hosts[0].paths[0].path == "/"' "$app/helmrelease.yaml" >/dev/null
  printf 'ok: %s full-host Envoy OIDC contract (%s)\n' "$resource_name" "$state"
}
validate_full_host arrs listenarr listenarr listenarr listenarr 4545
validate_full_host arrs profilarr profilarr profilarr profilarr 6868
validate_full_host services homarr homarr dash homarr 7575
