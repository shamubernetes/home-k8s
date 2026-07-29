# Zoo Fleet

Zoo Fleet is the generic firmware control plane for The Zoo. It uses the shared
PostgreSQL 17 service, Mosquitto at `mqtt.thezoo.house`, and the dedicated
Unraid firmware release share.

## Release-first deployment boundary

The Flux Kustomization is intentionally suspended and the HelmRelease is kept
as `app/helmrelease.release-required.yaml.tmpl`. The existing `v0.1.1` image
does not contain the production startup work in FLEET-11, and no digest is
fabricated here.

To activate the first deployment:

1. Merge and publish a new stable Zoo Fleet tag.
2. Copy the exact multi-architecture image digest emitted by the release job.
3. Copy `helmrelease.release-required.yaml.tmpl` to `helmrelease.yaml` and
   replace `RELEASE_TAG@sha256:RELEASE_DIGEST` with that release tag and digest.
4. Add `./helmrelease.yaml` to `app/kustomization.yaml`.
5. Run `scripts/validate-app --offline iot/zoo-fleet` and
   `scripts/check-image-pins kubernetes/apps/iot/zoo-fleet`.
6. Remove `spec.suspend: true` from `ks.yaml` in the same reviewed change.

The image runs database migrations under a PostgreSQL advisory lock before
opening its HTTP listener. Missing migrations, an unavailable database, or
incomplete production configuration therefore keeps startup and readiness
failed instead of serving against an unknown schema.

## Required 1Password fields

Create a `zoo-fleet` item in the `Kubernetes` vault with:

- `ZOO_FLEET_POSTGRES_USER` and `ZOO_FLEET_POSTGRES_PASS`: dedicated database
  role credentials. External Secrets percent-encodes both values into
  `DATABASE_URL` while preserving the raw values required by `postgres-init`.
- `PUBLISH_TOKEN_SHA256` and `OPERATOR_TOKEN_SHA256`: lowercase SHA-256
  digests of the two API bearer tokens. Keep the raw tokens in their callers,
  not in the server item.
- `SIGNING_KEY_ID`: stable public signing-key identifier.
- `SIGNING_PUBLIC_KEY_BASE64`: the 32-byte Ed25519 public key in standard
  base64.
- `SIGNING_PRIVATE_KEY_REF`: the `op://` reference used by `fleetctl` for the
  matching private key. The service stores this policy reference and public
  key; it never reads the private key.

The existing `cloudnative-pg` item supplies `POSTGRES_SUPER_PASS` to the
short-lived `postgres-init` init container. The existing `mosquitto` item
supplies `ADMIN_USERNAME` and `ADMIN_PASSWORD`.

Zoo Fleet deliberately shares that Mosquitto administrative control-plane
identity because it continuously manages Dynamic Security clients, roles, and
ACLs for enrolled devices. It does not use or duplicate a separately
bootstrapped MQTT account. Device identities and one-time device credentials
remain distinct and are created through Mosquitto Dynamic Security during
enrollment; they are never sourced from the administrative 1Password item.

External Secrets materializes the required runtime values as
`zoo-fleet-secret`; no secret value belongs in Git.

## Storage and backup ownership

- PostgreSQL is part of the shared `postgres17` CloudNativePG cluster and is
  covered by that cluster's existing scheduled volume-snapshot and
  barman-cloud backups.
- Immutable firmware bytes live on the retained RWX PV backed by
  `10.100.47.100:/mnt/user/firmware-releases`. Kubernetes retains the PV, but
  it does not back up the Unraid share. Unraid snapshot/backup policy owns that
  data and must protect the share before the first release is published.
- A usable restore requires both PostgreSQL and the NFS release tree from a
  mutually consistent recovery point. Do not restore one side and assume Zoo
  Fleet will reconstruct the other.

## Upgrades and rollback

Every upgrade is a reviewed digest change. Flux uses a rolling update and Helm
rollback remediation. Zoo Fleet applies only forward, immutable SQL migrations
before becoming ready, so application rollback must use an older image that
remains compatible with the already-applied schema. Database migrations are
never rolled back automatically.

If a release fails startup or readiness, keep the prior digest in Git or revert
the digest change. Do not delete the retained NFS PVC, change immutable release
files, or manually edit `schema_migrations`.

Public HTTPS for `fleet.thezoo.house` is provided by the external ingress and
cluster TLS pattern. MQTT TLS remains owned by the Mosquitto deployment at
`mqtt.thezoo.house`.
