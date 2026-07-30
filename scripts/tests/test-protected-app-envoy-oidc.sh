#!/usr/bin/env bash
# shellcheck disable=SC2016 # Flux placeholders are intentionally literal in repository manifests.
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

identity_headers=(
  X-Auth-Request-User
  X-Auth-Request-Email
  X-Auth-Request-Groups
)

validate_split_app() {
  local app_dir=$1
  local resource_name=$2
  local host_name=$3
  local backend_name=$4
  local required=$5
  local app="kubernetes/apps/arrs/${app_dir}/app"
  local host="${host_name}.\${HOME_DOMAIN}"
  local oidc_item="${resource_name}-oidc"
  local oidc_secret="${resource_name}-oidc-secret"
  local browser_route="${resource_name}-envoy-browser"
  local api_route="${resource_name}-envoy-api"
  local dns_resource="${app}/service-envoy-dns.yaml"
  local resources=(
    externalsecret-oidc.yaml
    httproute-envoy-api.yaml
    httproute-envoy-browser.yaml
    securitypolicy-oidc.yaml
  )
  local present=0
  local resource

  for resource in "${resources[@]}"; do
    [[ -f "${app}/${resource}" ]] && ((present += 1))
  done

  if ((present == 0)); then
    if [[ $required == true ]]; then
      printf 'required protected-app contract is absent for %s\n' "$resource_name" >&2
      return 1
    fi
    return 0
  fi

  if ((present != ${#resources[@]})); then
    printf 'partial protected-app contract for %s (%d/%d resources)\n' \
      "$resource_name" "$present" "${#resources[@]}" >&2
    return 1
  fi

  for resource in "${resources[@]}"; do
    grep -Fqx -- "- ./${resource}" "$app/kustomization.yaml"
  done

  export host backend_name browser_route api_route resource_name oidc_item oidc_secret

  local common_route='
    .kind == "HTTPRoute" and
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
    (.spec.rules[0].backendRefs | length) == 1 and
    .spec.rules[0].backendRefs[0].group == "" and
    .spec.rules[0].backendRefs[0].kind == "Service" and
    .spec.rules[0].backendRefs[0].name == strenv(backend_name) and
    .spec.rules[0].backendRefs[0].port == 80 and
    (.spec.rules[0].filters | length) == 1 and
    .spec.rules[0].filters[0].type == "RequestHeaderModifier"
  '

  yq -e "${common_route} and
    .metadata.name == strenv(api_route) and
    .spec.rules[0].matches[0].path.value == \"/api\" and
    (.spec.rules[0].filters[0].requestHeaderModifier.remove | length) == 3 and
    .spec.rules[0].filters[0].requestHeaderModifier.remove[0] == \"${identity_headers[0]}\" and
    .spec.rules[0].filters[0].requestHeaderModifier.remove[1] == \"${identity_headers[1]}\" and
    .spec.rules[0].filters[0].requestHeaderModifier.remove[2] == \"${identity_headers[2]}\"
  " "$app/httproute-envoy-api.yaml" >/dev/null

  yq -e "${common_route} and
    .metadata.name == strenv(browser_route) and
    .spec.rules[0].matches[0].path.value == \"/\" and
    (.spec.rules[0].filters[0].requestHeaderModifier.remove | length) == 4 and
    .spec.rules[0].filters[0].requestHeaderModifier.remove[0] == \"Authorization\" and
    .spec.rules[0].filters[0].requestHeaderModifier.remove[1] == \"${identity_headers[0]}\" and
    .spec.rules[0].filters[0].requestHeaderModifier.remove[2] == \"${identity_headers[1]}\" and
    .spec.rules[0].filters[0].requestHeaderModifier.remove[3] == \"${identity_headers[2]}\"
  " "$app/httproute-envoy-browser.yaml" >/dev/null

  yq -e '
    .kind == "SecurityPolicy" and
    .metadata.name == strenv(oidc_item) and
    (.spec.targetRefs | length) == 1 and
    .spec.targetRefs[0].group == "gateway.networking.k8s.io" and
    .spec.targetRefs[0].kind == "HTTPRoute" and
    .spec.targetRefs[0].name == strenv(browser_route) and
    .spec.oidc.provider.issuer == "https://sso.${HOME_DOMAIN}" and
    .spec.oidc.clientIDRef.name == strenv(oidc_secret) and
    .spec.oidc.clientSecret.name == strenv(oidc_secret) and
    .spec.oidc.redirectURL == ("https://" + strenv(host) + "/oauth2/callback") and
    .spec.oidc.logoutPath == "/oauth2/logout" and
    (.spec.oidc.scopes | length) == 3 and
    .spec.oidc.scopes[0] == "openid" and
    .spec.oidc.scopes[1] == "profile" and
    .spec.oidc.scopes[2] == "email" and
    .spec.oidc.forwardAccessToken == false and
    .spec.oidc.forwardIDToken == null and
    .spec.oidc.passThroughAuthHeader == false and
    .spec.oidc.cookieConfig.sameSite == "Lax" and
    .spec.oidc.cookieNames.accessToken == (strenv(resource_name) + "-access-token") and
    .spec.oidc.cookieNames.idToken == (strenv(resource_name) + "-id-token")
  ' "$app/securitypolicy-oidc.yaml" >/dev/null

  yq -e '
    .kind == "ExternalSecret" and
    .metadata.name == strenv(oidc_item) and
    .spec.target.name == strenv(oidc_secret) and
    .spec.target.creationPolicy == "Owner" and
    .spec.target.deletionPolicy == "Retain" and
    (.spec.data | length) == 2 and
    .spec.data[0].secretKey == "client-id" and
    .spec.data[0].remoteRef.key == strenv(oidc_item) and
    .spec.data[0].remoteRef.property == "client-id" and
    .spec.data[1].secretKey == "client-secret" and
    .spec.data[1].remoteRef.key == strenv(oidc_item) and
    .spec.data[1].remoteRef.property == "client-secret"
  ' "$app/externalsecret-oidc.yaml" >/dev/null

  local app_controller api_controller state
  app_controller=$(yq -r '.spec.values.ingress.app.annotations."external-dns.alpha.kubernetes.io/controller" // ""' "$app/helmrelease.yaml")
  api_controller=$(yq -r '.spec.values.ingress.api.annotations."external-dns.alpha.kubernetes.io/controller" // ""' "$app/helmrelease.yaml")

  if [[ -f $dns_resource ]]; then
    [[ $app_controller == ignore ]]
    [[ $api_controller == ignore ]]
    grep -Fqx -- '- ./service-envoy-dns.yaml' "$app/kustomization.yaml"
    yq -e '
      .kind == "Service" and
      .metadata.name == (strenv(resource_name) + "-envoy-dns") and
      .metadata.annotations."external-dns.alpha.kubernetes.io/hostname" == strenv(host) and
      .metadata.annotations."external-dns.alpha.kubernetes.io/target" == "${IPAM_IP_ENVOY_INTERNAL}" and
      .spec.type == "ExternalName" and
      .spec.externalName == "envoy-internal.network.svc.cluster.local"
    ' "$dns_resource" >/dev/null
    state=cutover
  else
    [[ -z $app_controller ]]
    [[ -z $api_controller ]]
    if grep -Fqx -- '- ./service-envoy-dns.yaml' "$app/kustomization.yaml"; then
      printf 'DNS service is listed without a manifest for %s\n' "$resource_name" >&2
      return 1
    fi
    state=staged
  fi

  yq -e '
    .spec.values.ingress.app.annotations."nginx.ingress.kubernetes.io/auth-url" == "http://oauth2-proxy.arrs.svc.cluster.local:4180/oauth2/auth" and
    .spec.values.ingress.app.hosts[0].paths[0].path == "/" and
    .spec.values.ingress.api.hosts[0].paths[0].path == "/api"
  ' "$app/helmrelease.yaml" >/dev/null

  printf 'ok: %s Envoy OIDC contract (%s)\n' "$resource_name" "$state"
}

validate_split_app sonarr sonarr sonarr sonarr true
validate_split_app radarr radarr radarr radarr false
validate_split_app radarr-3d radarr-3d radarr-3d radarr-3d false
validate_split_app prowlarr prowlarr prowlarr prowlarr false
validate_split_app sabnzbd sabnzbd sab sabnzbd false
validate_split_app bazarr bazarr bazarr bazarr false
validate_split_app whisparr whisparr whisparr whisparr false
