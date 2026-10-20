# pocket-id daily backup candidate

Status: implemented source candidate, not registered with Flux, not migrated.
See `INTEGRATION.md` for the tested acceleration changes and exact execution order.
Do not apply this root before its provisioning and recovery gates pass. The
existing `app/` and `ks.yaml` are unchanged. No ExternalSecret points at an
invented 1Password item. No credentials were read or provisioned in this work.

## Shared prerequisites

Dedicated backend identities were provisioned and verified by the independent
identity lane. This candidate consumes only its nonsecret metadata, uses the exact
private NAS share and dedicated R2 bucket, and includes narrowly templated backend
ExternalSecrets. It remains unregistered and ESO sync is not claimed. Native
API/DB credentials and shared admission remain separate activation gates:

| Namespace | Secret | Required keys | Scope |
|---|---|---|---|
| security | pocket-id-kopiur-nas | KOPIA_PASSWORD, KOPIA_RCLONE_CONFIG | Unique NAS Kopia password and SMB identity restricted to private share kopiur-pocket-id |
| security | pocket-id-kopiur-r2 | KOPIA_PASSWORD, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY | Independent R2 Kopia password and identity restricted to kopiur-pocket-id/kopiur/pocket-id/r2/ |

Generate passwords independently, keep original decryption keys in durable
escrow, and verify item/field metadata and resulting Secret key presence without
printing values. No `volsync-template` extraction, guest SMB identity or shared
API token is assumed. `backend-identity.json` records verified item/field metadata
from the provisioning handoff. No missing native API or DB-role Secret is invented.
The R2 endpoint follows the current Kopiur peer backend host, without a URL
scheme. Dedicated identity permissions still require real backend qualification.

The coordinator must extend all relevant shared Kopiur mover deadline selectors
for namespace `security`, policy `pocket-id-native`, repositories `pocket-id-nas-smb` and
`pocket-id-r2`, and replication `pocket-id-native-nas-to-r2` before registration.
Cover capture, verify, replication, discovery/probe and applicable maintenance
jobs. Hook Jobs already have a 600-second deadline. Repository maintenance is
explicitly disabled pending its own qualified schedule, not backup capture.
Check Kopiur controller hook RBAC and namespace mover admission separately.
Shared policy changes are deliberately not part of this application-only PR.

Candidate capture is daily at 18:20 America/New_York,
replication daily at 20:20, quick verification daily
at 21:20, deep verification Sunday at
22:20. These are natural later-today slots only if
the coordinator qualifies and registers the candidate before its slot. Never wait
overnight or change to hourly for qualification. `runOnCreate` is false.

Rendered CSI source identities are exact PVC names, never a broad selector:
- `/pvc/pocket-id-kopiur-capture`

Default point deletion is Retain; strict read-error and verification behavior is
preserved. No existing coverage is muted or retired. The coordinator still owns
real original NAS/R2 capture, lineage, native independent restore, exact-revision
rollout and any eventual VolSync retirement. Dry-run acceptance is not production
recovery qualification.

## State and native recovery

The live Pocket ID 2.18 distroless app mounts only `/tmp`. The legacy `pocket-id`
PVC is Bound but is not mounted by the application. Neither its VolSync sources
nor a new snapshot of that PVC cover the active identity service.

Actual state is PostgreSQL `pocket_id` on shared `postgres17`, RGW objects in
`pocket-id-bucket`, and `ENCRYPTION_KEY` from the approved per-app Secret
`pocket-id-secret`. The approved key provenance is item `pocket-id`, field
`POCKET_ID_ENCRYPTION_KEY`. Runtime DB connection uses the shared pooler;
the backup role uses the direct writer and TLS. CNPG protects the shared cluster
separately but cannot replace a paired native dump, original key and RGW bytes.

The hook runs an isolated Job. AWS CLI first records a bucket inventory and
copies all objects using the app's existing bucket Secret/ConfigMap references.
The PostgreSQL init container performs `pg_dump -Fc`, validates the archive and
copies only `ENCRYPTION_KEY` from an explicitly projected Secret key, not the
app Secret's shared superuser or SMTP credentials. The last container compares
the bucket's key/ETag/size inventory across the database dump and rejects changes.
The candidate captures the resulting dedicated PVC, not the unused legacy PVC.
All exporter processes run as UID/GID 568 with no API token, no added capability
and a 600-second deadline. There is no application lifecycle hook.

The inventory comparison detects common object churn, but it is NOT proof of
transactional DB/object-store equality. Object versioning, conditional reads,
manifest-to-database reference checks and concurrent upload/delete behavior still
need native qualification. No claim of global consistency is made. The bundle
explicitly records `cross_store_atomicity_qualified: false`.

Additional provisioning:
- Keep qualification single-writer and verify controller/schedule ownership.
  `Forbid` serializes the daily schedule, not unrelated manual Snapshot requests.
  The shared pending/current export directory is not qualified for overlapping
  hook Jobs. Do not issue concurrent manual captures or enable a second schedule.
- Create a unique read-only PostgreSQL backup role restricted to database
  `pocket_id`. Project `PGUSER`, `PGPASSWORD`, `PGDATABASE=pocket_id` into
  `pocket-id-kopiur-postgres`. Do not reuse Pocket ID's runtime owner or the CNPG
  superuser. No verified backup-role Secret currently exists.
- Verify the existing per-app RGW identity can list/read its own bucket. Its
  Secret/ConfigMap names and production references are verified, not its actual
  read permissions. No other app's S3/API credentials are introduced.

Additional recovery gates:
- Native restore the exact dump, matching RGW bytes and original encryption key
  into isolated same-image Pocket ID. Compare user/client/passkey metadata and
  JWKS identity, decrypt original state, and verify a representative original
  credential/auth path. A newly generated key must fail the original-key test.
- Prove DB/object reference equality under concurrent changes with no writer
  fence. Until then this remains a draft even after credentials are provisioned.
- Retain the legacy PVC and incumbent recovery points. Retirement is out of scope.
