# Service-scoped backup identities

Kaneo K8S-92 is the durable owner. This is an identity prerequisite, not evidence
that the full migration or application recovery contract is accepted.

## Scope and preservation

This candidate qualifies the 19 already-implemented NAS/R2 repository contracts:
canary, kometa, listenarr, tautulli, sabnzbd, wizarr, homarr, profilarr,
audiobookshelf, seerr, cwa-bdl, changedetection, radarr, radarr-3d, sonarr,
whisparr, grimmory, tubearchivist, and bazarr. The last two reuse their previously
provisioned identities. The mixed-database candidate supplies Grimmory and
TubeArchivist manifests; the integration phase must compose the candidates.
This prerequisite does not invent credentials for other inventory applications
whose capture/repository contracts have not yet been implemented.

Existing encryption passwords are preserved byte-for-byte. The 12 incumbent
escrow items keep their original NAS/R2 passwords, and all legacy repository
bytes remain in Backups/kopiur/<app>-smb and storage-backup. New isolated NAS
roots use Backups/kopiur/<app>-private-smb, and new R2 roots use kopiur-<app>.
Do not deploy these retargeted Repository specs until the integrated release
proves a direct restore of both retained legacy points and the new generations.
The original global Backups share, unrelated users, incumbent VolSync credentials,
retention, and schedules are not changed by this phase.

## Minimal permissions and delivery

Each NAS identity is kp-<app>, with a distinct UID, no login shell, a persistent
Unraid account row, and exactly one non-guest hidden share, kopiur-<app>. Its
private directory is mode 0700, owned by that UID. Global Samba invalid-users
prevents the new identity from using unrelated shares; the owned share explicitly
allows only that identity and disables guest access. Existing global users are
preserved, and configuration reload does not restart Samba. An account may read,
list, write, rename, and delete objects only inside its own backup root.

Each R2 identity has one active allow policy, permission group
2efd5506f9c8494dacb1fa10a3e7d5b6, and exactly one resource:
com.cloudflare.edge.r2.bucket.0834f4848c703f1fcf5b524bdf5f1722_default_kopiur-<app>.
This is object read/write for one isolated bucket, not account administration or
shared storage-backup access. The scoped identity must support list, HEAD,
put/get, multipart complete/abort, and deletion of its own test objects.

The Kubernetes vault item kopiur-<app> contains NAS_USERNAME, NAS_PASSWORD,
NAS_SHARE, NAS_UID, NAS_RCLONE_CONFIG, NAS_KOPIA_PASSWORD, R2_ACCESS_KEY_ID,
R2_SECRET_ACCESS_KEY, R2_TOKEN_ID, R2_ACCOUNT_ID, R2_BUCKET, and R2_KOPIA_PASSWORD.
ExternalSecrets use the existing op-secret-store. Source contains field references,
not secret values. Adding unused fields to incumbent items does not change their
still-deployed literal guest/shared credential templates. Deploying the reviewed
candidate is the separate, gated cutover.

## PostgreSQL identities

The five source login names are kopiur_bazarr, kopiur_radarr, kopiur_radarr_3d,
kopiur_sonarr, and kopiur_whisparr. The allowlisted database sets are respectively
bazarr, radarr_main, radarr_3d_main, sonarr_main, and the two databases
whisparrv3_main plus whisparrv3_logs. Only these six databases are grant targets.
The corresponding application-owner migrations must stay within this contract.

Use the app's PG_BACKUP_USER and PG_BACKUP_PASSWORD fields from the same vault
item, never the application or CNPG superuser identity in a mover. New roles are
LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT,
with default_transaction_read_only=on, CONNECT, public schema USAGE, and SELECT
on current/future owner tables and sequences. No broader role membership,
write/column/sequence grants, security-definer execution, RLS, large objects,
unknown schemas, or unexpected future write ACLs are accepted. Transactions have
5-second lock and 30-second statement timeouts and fail without widening grants.

Bazarr retains its app-local reviewed grants Job/SQL. The other four consume
kubernetes/components/kopiur-postgres/provision.sql, with the explicit
APPLICATION_NAME, APPLICATION_OWNER, BACKUP_USER, and BACKUP_PASSWORD contract.
The live grant/readback phase must resolve the actual primary and owner first.
Any failed preflight or transaction is a concrete application-level gate, not a
claim that the provider denied unrelated transport operations.

The native ARC fixtures execute these exact SQL sources using fresh app-specific
backup roles. They prove idempotency, reject unowned collisions, elevated and
inherited roles, table/column writes, sequence USAGE, and future write ACLs;
then capture and restore through the resulting read-only identity. The Whisparr
role is shared only by its explicitly allowlisted main/log database pair.

## Real backend encryption qualification

The trusted-main recovery workflow's identities suite runs
scripts/kopiur-identity-transport.py --serve on the existing ghar-set-zoo ARC.
The operator first verifies the exact candidate commit, GitHub run, allocated
runner pod UID and owned socket. It resolves one approved vault item at a time
and sends only that item's fields through stdin to that runner's private socket.
No service credential is added to GitHub Secrets, written to source/fixtures,
or sent as a process argument. Container root filesystems are read-only;
configuration, temporary/cache/log files, and synthetic source bytes are tmpfs.

Each service's NAS and R2 tests create a distinct UUID-owned synthetic Kopia
repository, remove the producer, deny a wrong encryption password, and restore
4096 bytes in a fresh container directly from that backend. NAS also proves guest
share access denied. The test uses the currently deployed mover digest and
never reads production application data. Cleanup terminates only UUID-labeled
containers, purges only the exact owned fixture paths, and verifies removal.
This proves encryption and transport identities, not populated application
recovery, CSI generation binding, production timing, or retention acceptance.

## Rotation and release gates

Rotate one service and one backend at a time. Issue replacement backend access
credentials with the same single-share/single-bucket permissions, escrow through
1Password, refresh ESO, and test both backup and an independent restore before
revoking the old access credential. Keep encryption passwords stable for retained
repositories. Encryption password changes require Kopia's supported password
change/rekey procedure and independent restores of every retained repository;
never overwrite an escrow field to simulate re-encryption.

Do not delete legacy repositories, users, tokens, or VolSync policy state during
this identity phase. Protected review/merge, scoped maintenance, exact Flux
revision, application health, full-state/paired-generation restore, independent
NAS/R2 recovery, and retained rollback are still required for service release.
The remaining application lanes and full inventory are separate serial work.

Sanitized execution evidence, actual provider responses, vault target IDs,
UIDs, exact commits, checks, and test receipts live in the K8S-92 handoff. Raw
credentials and private application archives must never enter that evidence.
