# Workstream H, Envoy Gateway native OIDC migration

Date: 2026-07-29
Status: H1 inventory complete; isolated Sonarr canary implementation in progress

## Scope boundary

The current cluster has 72 Ingress objects. Workstream H initially owns 10 OAuth2 Proxy-protected browser Ingresses, eight separate machine/API Ingresses, and the OAuth2 Proxy Ingress. Fifty-three unrelated Ingresses remain after that protected set moves, including 20 Ingresses with NGINX-specific behavior. OAuth2 Proxy retirement and NGINX controller retirement are therefore separate gates.

## Protected application inventory

| Application | Browser route | Current unauthenticated machine route | Confirmed machine behavior |
|---|---|---|---|
| Bazarr | `/` | `/api` | Uses Sonarr and Radarr through cluster-local Services and API keys; Hermes media tooling also uses its API. |
| Listenarr | `/` | None | No separate machine route found. |
| Profilarr | `/` | `/api` | No configured Arr instances. Do not reproduce the blanket bypass until a caller is demonstrated and the minimum paths are known. |
| Prowlarr | `/` | `/api` | Sonarr, Radarr, Radarr-3D, and Whisparr use cluster-local Prowlarr endpoints with API keys. |
| Radarr | `/` | `/api` | Prowlarr and other integrations use the cluster-local Service with an API key. |
| Radarr-3D | `/` | `/api` | Prowlarr uses the cluster-local Service with an API key. |
| SABnzbd | `/` | `/api` | Sonarr and Radarr use the cluster-local Service with an API key; `/api?mode=version` is the pod health path. |
| Sonarr | `/` | `/api` | Prowlarr, Bazarr, and other integrations use the cluster-local Service with an API key. |
| Whisparr | `/` | `/api` | Prowlarr uses the cluster-local Service with an API key. |
| Homarr | `/` | None | No separate machine route found. |

Current application manifests do not consume `X-Auth-Request-User`, `X-Auth-Request-Email`, or `X-Auth-Request-Groups`. Gateway routes must remove these client-supplied headers so a downstream application cannot accidentally trust spoofed identity during or after migration.

## Authorization contract

Authentication alone is insufficient. Each native OIDC client must be restricted in Pocket ID to the `protected-apps` group. That group contains exactly the identity allowed by the current OAuth2 Proxy email allowlist; another enabled identity remains outside the group for denial testing.

Do not replace this with an unrestricted Pocket ID client. Authorization parity must be verified independently for each client.

## Sonarr canary design

The canary hostname is `sonarr-oidc-canary.thezoo.house`. It does not replace `sonarr.thezoo.house` and does not change the production Sonarr Ingress or DNS record.

Resources:

- `HTTPRoute/sonarr-oidc-canary-browser`, attached to `Gateway/network/envoy-internal`, routes `/` to the existing Sonarr Service.
- `SecurityPolicy/sonarr-oidc-canary`, attached only to the browser route, performs native OIDC with the dedicated Pocket ID client.
- `HTTPRoute/sonarr-oidc-canary-api` routes `/api` without OIDC so API-key clients remain non-browser clients. Gateway longest-prefix matching keeps `/api` separate from `/`.
- Both routes remove legacy `X-Auth-Request-*` headers before forwarding.
- `ExternalSecret/sonarr-oidc-canary` materializes the Envoy-required `client-id` and `client-secret` fields from the Kubernetes vault.
- `Service/sonarr-oidc-canary-dns` is a removable ExternalName DNS declaration. UniFi ExternalDNS v0.21.0 watches Services and honors their `target` annotation before Service-type target discovery, so the canary record points to `${IPAM_IP_ENVOY_INTERNAL}`. It must not alias `internal.${HOME_DOMAIN}`, which still points to the NGINX internal VIP. HTTPRoute source expansion is not required.
- Existing wildcard TLS and the existing internal Gateway are reused. No ReferenceGrant is required because HTTPRoutes, backend Service, SecurityPolicy, and credential Secret are all in `arrs`; the Gateway listener already permits routes from all namespaces.

The Pocket ID client requires PKCE S256 and uses `https://sonarr-oidc-canary.thezoo.house/oauth2/callback`. The live Envoy 1.38.1 OAuth2 filter generates a PKCE verifier and S256 challenge for every OAuth flow. OIDC cookies are host-only, Secure, HTTP-only, SameSite=Lax, and use canary-specific access and ID token names. Access and ID tokens are not forwarded upstream.

## Acceptance matrix

All items must pass before production Sonarr routing changes:

1. Unauthenticated browser request returns an OIDC redirect to Pocket ID with the dedicated client ID, exact callback URL, state, nonce cookie, and PKCE S256 challenge.
2. The allowed Pocket ID identity completes login and reaches Sonarr.
3. An enabled identity outside `protected-apps` is denied by Pocket ID.
4. A request with a valid Sonarr API key reaches `/api/v3/system/status` without an OIDC redirect.
5. The same API request without an API key is rejected by Sonarr.
6. Browser/API requests carrying spoofed legacy identity headers do not cause an authentication or authorization bypass.
7. Callback requests with missing, malformed, or mismatched state/CSRF data are rejected.
8. `/oauth2/logout` clears Envoy session cookies and invokes Pocket ID logout; the next root request requires authentication.
9. Sonarr browser operation and its SignalR/WebSocket traffic work through the authenticated route.
10. Cookies remain host-only and are not presented to production Sonarr or other application hosts.
11. Removing the five canary resources leaves production Sonarr, its Ingresses, and its DNS record unchanged.

## Rollback

Rollback before production cutover is deletion of the canary manifests from the Sonarr app kustomization followed by Flux reconciliation. This removes the canary routes, SecurityPolicy, generated Secret ownership, and LAN DNS alias without changing the production Ingresses. The dedicated Pocket ID client and 1Password item can then be archived after route removal is verified.

Production cutover requires a separate change and a fresh rollback capture. OAuth2 Proxy remains deployed until every protected production host has passed its own acceptance matrix.
