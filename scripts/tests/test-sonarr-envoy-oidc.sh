#!/usr/bin/env bash
# shellcheck disable=SC2016 # Flux placeholders are intentionally literal in repository manifests.
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

app=kubernetes/apps/arrs/sonarr/app
host='sonarr.${HOME_DOMAIN}'
export host

for resource in \
  externalsecret-oidc.yaml \
  httproute-envoy-api.yaml \
  httproute-envoy-browser.yaml \
  securitypolicy-oidc.yaml; do
  grep -Fqx -- "- ./${resource}" "$app/kustomization.yaml"
done

common_route='
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
  .spec.rules[0].backendRefs[0].name == "sonarr" and
  .spec.rules[0].backendRefs[0].port == 80 and
  (.spec.rules[0].filters | length) == 1 and
  .spec.rules[0].filters[0].type == "RequestHeaderModifier"
'

yq -e "${common_route} and
  .metadata.name == \"sonarr-envoy-api\" and
  .spec.rules[0].matches[0].path.value == \"/api\" and
  (.spec.rules[0].filters[0].requestHeaderModifier.remove | length) == 3 and
  .spec.rules[0].filters[0].requestHeaderModifier.remove[0] == \"X-Auth-Request-User\" and
  .spec.rules[0].filters[0].requestHeaderModifier.remove[1] == \"X-Auth-Request-Email\" and
  .spec.rules[0].filters[0].requestHeaderModifier.remove[2] == \"X-Auth-Request-Groups\"
" "$app/httproute-envoy-api.yaml" >/dev/null

yq -e "${common_route} and
  .metadata.name == \"sonarr-envoy-browser\" and
  .spec.rules[0].matches[0].path.value == \"/\" and
  (.spec.rules[0].filters[0].requestHeaderModifier.remove | length) == 4 and
  .spec.rules[0].filters[0].requestHeaderModifier.remove[0] == \"Authorization\" and
  .spec.rules[0].filters[0].requestHeaderModifier.remove[1] == \"X-Auth-Request-User\" and
  .spec.rules[0].filters[0].requestHeaderModifier.remove[2] == \"X-Auth-Request-Email\" and
  .spec.rules[0].filters[0].requestHeaderModifier.remove[3] == \"X-Auth-Request-Groups\"
" "$app/httproute-envoy-browser.yaml" >/dev/null

yq -e '
  .kind == "SecurityPolicy" and
  .metadata.name == "sonarr-oidc" and
  (.spec.targetRefs | length) == 1 and
  .spec.targetRefs[0].group == "gateway.networking.k8s.io" and
  .spec.targetRefs[0].kind == "HTTPRoute" and
  .spec.targetRefs[0].name == "sonarr-envoy-browser" and
  .spec.oidc.provider.issuer == "https://sso.${HOME_DOMAIN}" and
  .spec.oidc.clientIDRef.name == "sonarr-oidc-secret" and
  .spec.oidc.clientSecret.name == "sonarr-oidc-secret" and
  .spec.oidc.redirectURL == "https://sonarr.${HOME_DOMAIN}/oauth2/callback" and
  .spec.oidc.logoutPath == "/oauth2/logout" and
  (.spec.oidc.scopes | length) == 3 and
  .spec.oidc.scopes[0] == "openid" and
  .spec.oidc.scopes[1] == "profile" and
  .spec.oidc.scopes[2] == "email" and
  .spec.oidc.forwardAccessToken == false and
  .spec.oidc.forwardIDToken == null and
  .spec.oidc.passThroughAuthHeader == false and
  .spec.oidc.cookieConfig.sameSite == "Lax" and
  .spec.oidc.cookieNames.accessToken == "sonarr-access-token" and
  .spec.oidc.cookieNames.idToken == "sonarr-id-token"
' "$app/securitypolicy-oidc.yaml" >/dev/null

yq -e '
  .kind == "ExternalSecret" and
  .metadata.name == "sonarr-oidc" and
  .spec.target.name == "sonarr-oidc-secret" and
  .spec.target.creationPolicy == "Owner" and
  .spec.target.deletionPolicy == "Retain" and
  (.spec.data | length) == 2 and
  .spec.data[0].secretKey == "client-id" and
  .spec.data[0].remoteRef.key == "sonarr-oidc" and
  .spec.data[0].remoteRef.property == "client-id" and
  .spec.data[1].secretKey == "client-secret" and
  .spec.data[1].remoteRef.key == "sonarr-oidc" and
  .spec.data[1].remoteRef.property == "client-secret"
' "$app/externalsecret-oidc.yaml" >/dev/null

app_controller=$(yq -r '.spec.values.ingress.app.annotations."external-dns.alpha.kubernetes.io/controller" // ""' "$app/helmrelease.yaml")
api_controller=$(yq -r '.spec.values.ingress.api.annotations."external-dns.alpha.kubernetes.io/controller" // ""' "$app/helmrelease.yaml")
dns_resource="$app/service-envoy-dns.yaml"

if [[ -f $dns_resource ]]; then
  [[ $app_controller == ignore ]]
  [[ $api_controller == ignore ]]
  grep -Fqx -- '- ./service-envoy-dns.yaml' "$app/kustomization.yaml"
  yq -e '
    .kind == "Service" and
    .metadata.name == "sonarr-envoy-dns" and
    .metadata.annotations."external-dns.alpha.kubernetes.io/hostname" == "sonarr.${HOME_DOMAIN}" and
    .metadata.annotations."external-dns.alpha.kubernetes.io/target" == "${IPAM_IP_ENVOY_INTERNAL}" and
    .spec.type == "ExternalName" and
    .spec.externalName == "envoy-internal.network.svc.cluster.local"
  ' "$dns_resource" >/dev/null
  state=cutover
else
  [[ -z $app_controller ]]
  [[ -z $api_controller ]]
  if grep -Fqx -- '- ./service-envoy-dns.yaml' "$app/kustomization.yaml"; then
    printf 'DNS service is listed without a manifest\n' >&2
    exit 1
  fi
  state=staged
fi

# NGINX remains configured as a rollback path in both valid states.
yq -e '
  .spec.values.ingress.app.annotations."nginx.ingress.kubernetes.io/auth-url" == "http://oauth2-proxy.arrs.svc.cluster.local:4180/oauth2/auth" and
  .spec.values.ingress.app.hosts[0].paths[0].path == "/" and
  .spec.values.ingress.api.hosts[0].paths[0].path == "/api"
' "$app/helmrelease.yaml" >/dev/null

printf 'ok: production Sonarr Envoy OIDC contract (%s)\n' "$state"
