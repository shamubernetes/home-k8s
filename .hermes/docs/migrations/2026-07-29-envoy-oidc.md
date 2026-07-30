# Workstream H, Envoy Gateway native OIDC migration

Date: 2026-07-29
Status: H1 and isolated H2 complete; production Sonarr cut over to Envoy; authenticated production browser acceptance pending

## Scope boundary

The current cluster has 72 Ingress objects. Workstream H initially owns 10 OAuth2 Proxy-protected browser Ingresses, eight separate machine/API Ingresses, and the OAuth2 Proxy Ingress. Fifty-three unrelated Ingresses remain after that protected set moves, including 20 Ingresses with NGINX-specific behavior. OAuth2 Proxy retirement and NGINX controller retirement are therefore separate gates.

## Current shared authentication boundary

All ten browser hosts currently use the same NGINX/OAuth2 Proxy design:

- Unauthenticated browser requests redirect to same-host `/oauth2/start` with the original URI.
- Same-host `/oauth2/*` routes to the shared OAuth2 Proxy.
- OAuth2 Proxy authenticates against Pocket ID with PKCE, permits Pocket ID's `email_verified=false`, enforces the existing email allowlist, and emits `X-Auth-Request-*` headers.
- Native Envoy OIDC must preserve deep-link return, callback/logout behavior, unverified-email compatibility, and the authorization boundary. Successful authentication alone is not parity.

Primary source: `kubernetes/apps/arrs/oauth2-proxy/app/helmrelease.yaml` and each protected application's ingress block.

## Protected application route and backend-auth matrix

| Application | Browser route under Envoy | Machine/API route | Verified backend behavior and migration rule |
|---|---|---|---|
| Bazarr | OIDC on `/`, except the more-specific API route | `/api/*` | Uses `X-API-KEY`; missing and invalid keys return 401. Bazarr calls Sonarr and Radarr through cluster-local Services with API keys. |
| Listenarr | OIDC on the entire host, including `/api`, SPA assets, and SignalR/WebSockets | No safe bypass currently | Live `AuthenticationRequired=false`; `/api/library` returned 200 with missing, invalid, and valid keys. Do not create an unprotected `/api` route unless backend enforcement is enabled and proven. |
| Profilarr | OIDC on the entire host for the next migration design | Current NGINX `/api/*` bypass is unsafe and must not be copied | Manifest sets `AUTH=off`; live status, database, and Arr API operations returned 200 with no or invalid key. No Arr instances are registered. Hermes sends `X-Api-Key`, but Profilarr does not currently enforce it. |
| Prowlarr | OIDC on `/`, except API | `/api/v1/*` under `/api` | Missing and invalid API keys return 401. Its Radarr, Radarr-3D, Sonarr, and Whisparr applications use cluster-local Service DNS and API keys. |
| Radarr | OIDC on `/`, except API | `/api/v3/*` under `/api` | Missing and invalid API keys return 401. Prowlarr and Homarr use Service DNS; downloads go to SABnzbd category `movies`. |
| Radarr-3D | OIDC on `/`, except API | `/api/v3/*` under `/api` | Missing and invalid API keys return 401. Prowlarr and Homarr use Service DNS. No live download client is configured. |
| SABnzbd | OIDC on UI `/`, except API | `/api?mode=...` | Query `apikey` protects queue, history, and other sensitive operations with 403 on missing or invalid keys. `mode=version` is intentionally anonymous. Callers use Service DNS. |
| Sonarr | OIDC on `/`, except API | `/api/v3/*` under `/api` | Missing and invalid API keys return 401. Prowlarr, Bazarr, Homarr, and Hermes use API keys; downloads go to SABnzbd category `tv`. |
| Whisparr | OIDC on `/`, except API | `/api/v3/*` under `/api` | Missing and invalid API keys return 401. Prowlarr uses Service DNS; downloads go to SABnzbd category `adult`. |
| Homarr | OIDC on the entire host, including application `/api/*` | No broad bypass; exact anonymous liveness/readiness paths may be considered separately | Homarr's browser APIs share `/api`, so the prefix cannot be exempted. Its server-side integrations use cluster-local Service DNS and stored backend credentials. |

Current manifests do not consume `X-Auth-Request-User`, `X-Auth-Request-Email`, or `X-Auth-Request-Groups`. Every migrated browser and machine route must still remove these client-supplied headers so future downstream changes cannot accidentally trust spoofed identity.

## Caller and flow matrix

| Caller | Destination and path | Credential and route behavior |
|---|---|---|
| Hermes typed media tool on the Mac | Bazarr, Profilarr, Prowlarr, Radarr, Radarr-3D, SABnzbd, and Sonarr through HTTPS machine routes | Uses service-specific API-key headers or SABnzbd's `apikey` query field. Profilarr is presently anonymous despite the header. |
| Prowlarr | Radarr, Radarr-3D, Sonarr, and Whisparr through `*.arrs.svc.cluster.local` | API key; never traverses browser OIDC. |
| Prowlarr | SABnzbd Service `/api` | API key, category `prowlarr`. |
| Bazarr | Sonarr and Radarr Services | API keys; never traverses browser OIDC. |
| Listenarr | Prowlarr Service Newznab endpoints and SABnzbd Service | Newznab credentials; SABnzbd API key and category `audiobooks`. |
| Radarr, Sonarr, and Whisparr | SABnzbd Service | API key and categories `movies`, `tv`, and `adult`. Radarr-3D has no current download client. |
| Homarr | Bazarr, Prowlarr, Radarr, Radarr-3D, SABnzbd, Sonarr, Plex, and AdGuard Services | Server-side credentials; never traverses browser OIDC. |
| Radarr, Sonarr, and Radarr-3D | Hermes Gateway `/webhooks/servarr-alerts`; Radarr and Sonarr also notify Helmarr | Outbound webhooks, unrelated to inbound app OIDC. |
| Whisparr | Stash Service | Cluster-local notification call. |
| Radarr and Sonarr | Plex API | Plex token for library updates. |

The `arrs` namespace currently has no NetworkPolicies, so cluster-local API credentials remain an important boundary. Adding route OIDC does not protect direct Service traffic.

## Authorization contract

Authentication alone is insufficient. Each native OIDC client must be restricted in Pocket ID to the `protected-apps` group. That group contains exactly the identity represented by the current OAuth2 Proxy allowlist; another enabled identity remains outside the group for denial testing. Allowlist values are not recorded here.

Do not replace this with an unrestricted Pocket ID client. Authorization parity must be verified independently for each client.

## Sonarr canary design and live state

The canary hostname is `sonarr-oidc-canary.thezoo.house`. It does not replace `sonarr.thezoo.house` and does not change the production Sonarr Ingress or DNS record.

Resources:

- `HTTPRoute/sonarr-oidc-canary-browser`, attached to `Gateway/network/envoy-internal`, routes `/` to the existing Sonarr Service.
- `SecurityPolicy/sonarr-oidc-canary`, attached only to the browser route, performs native OIDC with the dedicated Pocket ID client.
- `HTTPRoute/sonarr-oidc-canary-api` routes `/api` without OIDC. Gateway longest-prefix matching keeps it separate from `/`.
- Both routes remove legacy `X-Auth-Request-*` headers before forwarding.
- `ExternalSecret/sonarr-oidc-canary` materializes the exact Envoy `client-id` and `client-secret` keys from the Kubernetes vault.
- `Service/sonarr-oidc-canary-dns` is a removable ExternalDNS declaration targeting `${IPAM_IP_ENVOY_INTERNAL}`. It does not alias `internal.${HOME_DOMAIN}`, which still points to NGINX.
- Existing wildcard TLS and `envoy-internal` are reused. No ReferenceGrant is required because routes, policy, backend Service, and credential Secret are all in `arrs`; the Gateway listener permits routes from all namespaces.

The Pocket ID client requires PKCE S256 and registers both the exact authorization callback and root logout callback. The live Envoy 1.38.1 OAuth2 filter generates a verifier and S256 challenge. OIDC cookies are host-only, Secure, HttpOnly, and SameSite=Lax. Access and ID tokens are not forwarded upstream.

PR #4229 deployed the canary at merge revision `2a9f1c95abf12e898f1e0220bee510e80cd1b39b`. PR #4231 added its required CI contract at `4a04f876ed2aa5bd9901a1a91e0227cfd142ad3e`. Flux applied both. Kilian completed the allowed-user browser acceptance and reported all exercised behavior working; a nonmember test was accepted as not applicable, and a separate rollback drill was declined. On 2026-07-29 he requested removal because the route targets the real Sonarr backend. The canary resources, dedicated Pocket ID client, and 1Password item were therefore retired before production cutover. Production Sonarr subsequently moved to Envoy through the staged H3 process recorded below; both NGINX Ingresses remain available at internal `10.100.47.250` as rollback.

## Acceptance matrix

Automated items passed before DNS cutover. Interactive items remain required before starting the Radarr cohort:

1. [x] Unauthenticated browser request redirects to Pocket ID with the dedicated client ID, exact callback, state cookies, and PKCE S256.
2. [ ] The allowed Pocket ID identity completes login and returns to the original Sonarr deep link.
3. [ ] An enabled identity outside `protected-apps` is denied by Pocket ID.
4. [x] Valid header and query Sonarr API keys reach `/api/v3/system/status` without OIDC.
5. [x] Missing and invalid API keys are rejected without an HTML login redirect.
6. [x] Spoofed legacy identity headers do not bypass browser or API policy, and both routes carry explicit removal filters.
7. [x] Missing or malformed state/CSRF callback data is rejected.
8. [x] `/oauth2/logout` invokes Pocket ID's end-session endpoint. Full post-login cookie clearing remains part of the interactive check.
9. [ ] Authenticated UI, assets, browser API calls, and SignalR/WebSocket traffic work.
10. [x] Pre-login state cookies are host-only, Secure, HttpOnly, and SameSite=Lax. Authenticated session-cookie scope remains part of the interactive check.
11. [x] The retained NGINX browser and API routes pass direct checks at `10.100.47.250`; the GitOps rollback reverses only DNS ownership. A live production DNS rollback was not exercised.

## Rollback ownership

Rollback before production cutover is removal of the five declared canary objects from the Sonarr app kustomization followed by Flux reconciliation. This removes both HTTPRoutes, the SecurityPolicy, ExternalSecret-owned generated Secret, and DNS shim without changing production Ingresses. UniFi ExternalDNS `policy=sync` removes its owned LAN record. The dedicated Pocket ID client and 1Password item are archived only after Kubernetes and DNS cleanup are verified.

The canary targets the existing Sonarr Service, so it is an edge-routing canary, not an isolated application instance. Any application or database mutation performed through the canary affects production Sonarr data and is outside edge rollback.

## Production Sonarr cutover, 2026-07-30

The production migration used three protected, separately classified PRs:

- PR #4237 staged `HTTPRoute/sonarr-envoy-api`, `HTTPRoute/sonarr-envoy-browser`, `SecurityPolicy/sonarr-oidc`, and `ExternalSecret/sonarr-oidc` without changing DNS or NGINX. It merged as `a3f896e6321d1cb65c7ddbe842ca3f3c7166783b`.
- PR #4239 added `scripts/tests/test-sonarr-envoy-oidc.sh` to required Static Analysis. The contract accepts exactly the staged state or an atomic cutover state with one Envoy DNS publisher and both retained Ingresses excluded from ExternalDNS. It merged as `76a08db800591eecbc184e174b9f0196b3c409b2`.
- PR #4240 added `Service/sonarr-envoy-dns` and `external-dns.alpha.kubernetes.io/controller: ignore` to both NGINX Ingresses. It merged as `8236ce8afe35eaf290790ea7ef5e20a3628fac25`.

The dedicated `sonarr-envoy` Pocket ID client is confidential, PKCE-enabled, and restricted to the one-member Protected Apps group. `ExternalSecret/sonarr-oidc` reads only `client-id` and `client-secret` from the dedicated `sonarr-oidc` 1Password item. The generated Secret is Ready, both HTTPRoutes are Accepted with ResolvedRefs, and the SecurityPolicy is Accepted.

Before DNS cutover, direct Envoy testing at `10.100.47.248` passed PKCE S256, Secure/HttpOnly/SameSite=Lax state cookies, invalid-state rejection, spoofed-header denial, unauthenticated WebSocket protection, logout, and missing/invalid/valid API-key behavior. Konflate showed that PR #4240 changed only the two Ingress annotations, their HelmRelease values, and the DNS-only Service. No Deployment, Pod, PVC, VolSync, or storage resource changed.

After Flux applied `8236ce8a`, UniFi ExternalDNS moved `sonarr.thezoo.house` from NGINX `10.100.47.250` to Envoy `10.100.47.248`. The same Sonarr Pod remained Ready with zero restarts. The complete automated matrix passed again through normal DNS, and direct NGINX browser/API checks preserved rollback. Sonarr retained four pre-existing completed queue items and no active commands. SABnzbd, Prowlarr indexers, and Prowlarr's Sonarr application continue to use cluster-local Service DNS.

A real browser reached Pocket ID's `Sign in to Sonarr` interaction. No credential or passkey action was taken. The allowed-user production round trip, authenticated UI/assets/session-cookie behavior, and authenticated SignalR/WebSocket behavior remain the gate before Radarr begins.

Production rollback removes `service-envoy-dns.yaml` from the Sonarr Kustomization and removes `external-dns.alpha.kubernetes.io/controller: ignore` from both Ingresses. ExternalDNS then republishes the unchanged NGINX route at `10.100.47.250`. Do not remove the production routes, policy, Pocket ID client, or 1Password item during this rollback; they remain staged for diagnosis and retry.

## Remaining cohort unknowns

These do not block the Sonarr canary, but must be resolved before their respective migrations:

- Exact Listenarr SignalR hub paths.
- Intended Radarr-3D download workflow while no client is configured.
- Whether Profilarr authentication will be enabled or its browser and machine operations will remain entirely under OIDC.
- Exact Homarr health exceptions, if any are needed externally.

OAuth2 Proxy remains deployed until all protected production hosts pass. NGINX retirement remains a later, separate migration for the entire remaining Ingress estate.
