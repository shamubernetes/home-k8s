# home-assistant daily backup candidate

Status: implemented source candidate, not registered with Flux, not migrated.
See `INTEGRATION.md` for the tested acceleration changes and exact execution order.
Do not apply this root before its provisioning and recovery gates pass. The
existing `app/` and `ks.yaml` are unchanged. No ExternalSecret points at an
invented 1Password item. No credentials were read or provisioned in this work.

## Shared prerequisites

Provision independent encryption/backend identities for this application before
registration, using the approved 1Password and External Secrets operator path.
The candidate intentionally references required Secret contracts only:

| Namespace | Secret | Required keys | Scope |
|---|---|---|---|
| home-assistant | home-assistant-kopiur-nas | KOPIA_PASSWORD, KOPIA_RCLONE_CONFIG | Unique NAS Kopia password and SMB identity restricted to Backups/kopiur/home-assistant-smb |
| home-assistant | home-assistant-kopiur-r2 | KOPIA_PASSWORD, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY | Independent R2 Kopia password and identity restricted to storage-backup/kopiur/home-assistant/r2/ |

Generate passwords independently, keep original decryption keys in durable
escrow, and verify item/field metadata and resulting Secret key presence without
printing values. No `volsync-template` extraction, guest SMB identity or shared
API token is assumed. Secret absence cannot be solved by deploying a broken
ExternalSecret. Dedicated identity provisioning is a real unresolved prerequisite.
The R2 endpoint follows the current Kopiur peer backend host, without a URL
scheme. Dedicated identity permissions still require real backend qualification.

The coordinator must extend all relevant shared Kopiur mover deadline selectors
for namespace `home-assistant`, policy `home-assistant-native`, repositories `home-assistant-nas-smb` and
`home-assistant-r2`, and replication `home-assistant-native-nas-to-r2` before registration.
Cover capture, verify, replication, discovery/probe and applicable maintenance
jobs. Hook Jobs already have a 600-second deadline. Repository maintenance is
explicitly disabled pending its own qualified schedule, not backup capture.
Check Kopiur controller hook RBAC and namespace mover admission separately.
Shared policy changes are deliberately not part of this application-only PR.

Candidate capture is daily at 18:10 America/New_York,
replication daily at 20:10, quick verification daily
at 21:10, deep verification Sunday at
22:10. These are natural later-today slots only if
the coordinator qualifies and registers the candidate before its slot. Never wait
overnight or change to hourly for qualification. `runOnCreate` is false.

Rendered CSI source identities are exact PVC names, never a broad selector:
- `/pvc/home-assistant`
- `/pvc/home-assistant-git`
- `/pvc/home-assistant-kopiur-capture`

Default point deletion is Retain; strict read-error and verification behavior is
preserved. No existing coverage is muted or retired. The coordinator still owns
real original NAS/R2 capture, lineage, native independent restore, exact-revision
rollout and any eventual VolSync retirement. Dry-run acceptance is not production
recovery qualification.

## State and native recovery

The live application mounts `home-assistant` at `/config` and
`home-assistant-git` at `/git`. The former VolSync pair covers only `/config`.
The running configuration binds recorder `db_url` to `!env_var HASS_POSTGRES_URL`;
the app's existing ExternalSecret builds that URL from the shared CNPG item.
A native config archive does not contain that external PostgreSQL database.

The hook runs a separate short-lived Job, not a sidecar or a replacement app
Pod. Its first container calls the installed native WebSocket `backup/generate`
with local agent `backup.local`, waits for the uniquely named completed result,
downloads through `/api/backup/download/{backup_id}`, validates the outer native
metadata and nested config/auth payload, and stages it privately. Its final
container uses online `pg_dump -Fc`, then `pg_restore --list`. Both PVCs and this
bundle are sources. Original `.storage` auth, integrations, HACS/custom components,
Git state, native archive and recorder database are distinct recovery inputs.

Installed HA 2026.8.0 source was inspected. Native backup invokes recorder's
pre-backup hook, but `Recorder.lock_database()` returns immediately for non-SQLite
backends. Do not enable this candidate if recorder fell back to SQLite. No app
stop, restart, freeze, direct `lock_database` call or writer hold is introduced.
The manually declared qualification environment flag is a prerequisite assertion,
not runtime proof that PostgreSQL is actually connected.

Additional provisioning:
- Generate a new dedicated HA admin long-lived access token for native backup
  operations, in an app-specific backup identity. Native backup is admin-only in
  this installed version. Put it in 1Password through the approved operator path,
  then project only `token` into `home-assistant-kopiur-api`. Do not reuse Hermes,
  user-session, mobile-app, or another service's bearer token. There is no verified
  existing approved token in the current manifests, so no ExternalSecret is added.
- Create a distinct read-only PostgreSQL backup role for the actual recorder DB,
  not the runtime owner or shared superuser. Project only `PGUSER`, `PGPASSWORD`,
  `PGDATABASE` into `home-assistant-kopiur-postgres`. Determine the DB name from
  approved private configuration during provisioning, do not guess it.
- Establish retention/capacity for native local backups created by this Job. This
  candidate never deletes existing Home Assistant backups or changes native agent
  retention. Its capture PVC retains current/previous bundles only.

Additional recovery gates:
- Keep qualification single-writer and verify controller/schedule ownership.
  `Forbid` serializes the daily schedule, not unrelated manual Snapshot requests.
  The shared pending/current export directory is not qualified for overlapping
  hook Jobs. Do not issue concurrent manual captures or enable a second schedule.
- Prove effective PostgreSQL recorder connection and no recorder SQLite lock path,
  integration pre/post backup side effects, process continuity and real HA API
  availability during a capture. A stable Pod UID/restart counter is insufficient.
- Measure native archive, recorder dump and CSI timestamps and accept/reject their
  skew explicitly. Config and recorder exports are not one global transaction.
- Restore original config/auth/HACS/Git plus the same recorder dump into isolated
  same-image HA, with automation/device/network egress blocked. Prove original
  authentication and representative recorder history from NAS and R2.
