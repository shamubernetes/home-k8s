# Kopiur local SQLite recovery

The original cohort covers `arrs/cwa-bdl`, `arrs/sabnzbd`, and `arrs/listenarr`.
The media extension covers only `media/kometa` and `media/audiobookshelf`, with
its stricter version-2 artifact contract documented below. Keep both existing
VolSync streams for every app. A configured policy is not an accepted recovery
path until the independent NAS and R2 recovery drills pass.

## Recovery contract

| Application | PVC mount | SQLite database relative to mount | Other required state | Mover UID |
|---|---|---|---|---|
| CWA-BDL | `/config` | `users.db` | `settings.json`, `.flask_secret` | 0 |
| SABnzbd | `/config` | `admin/history1.db` | `sabnzbd.ini` | 568 |
| Listenarr | `/app/config` | `database/listenarr.db` | `config.json` | 0 |

The pre-snapshot hook uses SQLite's online backup API to copy committed state,
including committed WAL transactions, without replacing or modifying the live
database. It checks the copy with `PRAGMA integrity_check`, fsyncs it, and
publishes `.kopiur-consistent/database.sqlite3` and a SHA-256 `manifest.json`.
Each database copy has a 90-second deadline within the two-minute hook timeout.
Missing required files, malformed required JSON, a corrupt database, or a backup
error aborts the snapshot. The previous successful database copy remains usable
if a new SQLite copy fails before publication.

The directory is mode 0700 and recovery files are mode 0600. CWA-BDL and
Listenarr execute as root in their existing production containers, so their
movers run as root to read these files and the rest of the PVC. SABnzbd uses UID
568 for both. The existing `arrs` privileged-mover namespace authorization is
required. The production workload specifications are unchanged.

The original database and sidecars remain in the filesystem snapshot. They are
crash-consistent, as are non-SQLite state files. The verified SQLite copy is the
recovery source to promote before boot. This is not a transaction across the
SQLite database, configuration files, and a downloader's in-memory queue. The
SABnzbd database copy protects persisted history, not an atomic checkpoint of
active downloads. Media and download directories on NFS are outside these PVCs
and outside this recovery contract.

The policies are app-local to match the existing Kopiur deployments. The fixture
suite exercises the actual embedded Python in every policy, rather than a
separate implementation.

## Provisioning and delivery gates

1. Create a `Kubernetes` vault item `kopiur-<app>` for each app. Each item needs
   independently generated, concealed `NAS_KOPIA_PASSWORD` and
   `R2_KOPIA_PASSWORD` fields. Do not share repository passwords between apps or
   destinations. Verify field presence without displaying values.
2. The ExternalSecrets use the existing Kopiur storage arrangement: guest SMB
   under `Backups/kopiur/<app>-smb` and the existing backup R2 credential under
   `kopiur/<app>/r2/`. Confirm the exact existing credential's authorized scope
   before provisioning. Do not create a new service integration or broaden its
   permissions. The NAS and R2 encryption passwords are separate from transport
   credentials.
3. Deliver the separate `kopiur-mover-deadline` policy prerequisite first. Its
   config, maintenance, replication, and verification selectors must include
   all three apps and both repository names. Verify the live policy enforces
   `activeDeadlineSeconds: 3600` on cohort mover Jobs. The hook and staging
   timeouts do not bound the upload, and `concurrencyPolicy: Forbid` otherwise
   lets a stalled upload block the next daily backup.
   Run the fixture suite and all three canonical app validators, then require
   exact-head protected PR checks. Keep the privileged policy change separate
   from the application PR. The Talos image gate applies to the application PR.
4. Open and verify the maintenance window immediately before an authorized
   merge. Apply the complete cohort before validating individual applications.
5. Verify the full merge revision on the GitRepository and all three Flux
   Kustomizations. Require the six declared health checks per app and verify
   replication separately because its observed generation can remain stale.
6. Require a fresh snapshot and R2 copy for every app. Record repository,
   snapshot ID, source identity, successful hook execution, and replica lineage.
   Initial schedules deliberately have `runOnCreate: false`.

## Independent restore drill

For **each** app and **each** destination:

1. Restore the selected recovery point into a new disposable PVC. Never target
   the production PVC. Restore from NAS and R2 independently, using the matching
   repository password and without relying on the other destination.
2. Confirm the expected application files and `.kopiur-consistent` files exist.
   Validate the manifest version, expected database path, database checksum, and
   `PRAGMA integrity_check=ok`. Compare database tables and aggregate row counts
   between the two restored copies from the same source recovery point. Do not
   publish application records or configuration contents as evidence.
3. In the **disposable restore only**, preserve the original database and its
   `-wal`, `-shm`, and `-journal` files together outside their application paths.
   Promote the verified copy to the exact database path in the table. Set the
   original application's UID, GID, and mode, and fsync the promoted file and its
   parent. Do not leave stale SQLite sidecars beside the promoted database.
4. Before starting the restored app, install and verify deny-by-default ingress
   and egress restrictions. No production NFS mounts, real download clients,
   schedulers, notification destinations, or external writes are permitted. Use
   empty disposable directories for media/download mount paths. Do not point
   the restore at production database services.
5. Boot the exact production image digest with the production container command,
   required environment structure, and security context. Supply restored state
   and isolated substitute mount paths. For SABnzbd, disable downloading and
   scheduling in the disposable configuration before boot. No public route is
   needed. Probe through the isolated pod's loopback interface.
6. Require CWA-BDL `/api/health` on port 8084, SABnzbd `/api?mode=version` on port
   80, and Listenarr `/` on port 4545. Also verify application-level records or
   persisted settings from the promoted state. An HTTP 200 alone is insufficient.
7. Retain accepted source and replica recovery points. An R2 replicated point is
   retained, not described as pinned unless live controller status proves it.
   Remove only the owned disposable restore resources after acceptance. Verify
   production health, VolSync success, storage health, alert baseline, and closure
   of the exact owned maintenance window.

Do not accept shared-PostgreSQL applications through this PVC-only procedure.
Those applications require independent recovery of the database and PVC state
followed by an isolated combined boot.

## Local verification

```sh
python3 -m unittest discover -s scripts/tests -p test_kopiur_sqlite_cohort.py -v
scripts/validate-app arrs/cwa-bdl
scripts/validate-app arrs/sabnzbd
scripts/validate-app arrs/listenarr
```

The fixture suite uses synthetic SQLite databases, not production copies. It
proves hook behavior, not live backend credentials, a real NAS/R2 restore, or an
application boot. Record those separately in the migration task before marking
an app accepted.

## Kometa and Audiobookshelf extension

These two policies use NAS-first snapshots and R2 replication. Both retain the
existing VolSync resources and application HelmRelease unchanged. They add no
namespace annotations. Hook, snapshot, verification, replication, maintenance,
and repository movers use UID/GID 568, fsGroup 568, `runAsNonRoot: true`,
`allowPrivilegeEscalation: false`, and capabilities `drop: [ALL]`. No Kopiur
privileged-mover opt-in or DAC capability is required.

| Application | Source DB | Other PVC state | Restore PVC capacity | Installed backup API |
|---|---|---|---|---|
| Kometa | `/config/config.cache` | `config.yml`, collection YAMLs, `UUID`, all other PVC files | 1Gi | Python `sqlite3.Connection.backup` |
| Audiobookshelf | `/config/absdatabase.sqlite` | Entire `metadata` subdirectory and all other PVC files | 5Gi | `/app/node_modules/sqlite3`, `Database.backup` |

Both source connections open read-only. Each hook has a 90-second internal
deadline and a two-minute fail-closed workloadExec timeout. The Python hook
uses SIGALRM, backup progress and integrity-query progress checks. The Node
hook awaits the database open, backup initialization, every `step`, completion,
`finish`, integrity check and database close callback. The `Database.backup`
callback alone proves initialization, not completed copying. BUSY and LOCKED
steps retry only within the deadline. An asynchronous SQLite error, unfinished
copy, missing required state, symlink source or integrity error aborts the hook.
Neither hook stops or restarts the app or writes to the source DB.

The required Kometa config and UUID must be nonempty regular files. Audiobookshelf
requires a real metadata directory. These presence checks do not validate
application semantics. Configuration, metadata and other non-SQLite files are
crash-consistent in the CSI snapshot. The DB backup and metadata files are not
an atomic application-wide transaction. Media on NFS is outside this contract.

### Version-2 publication and freshness

The media hooks intentionally do not use the original cohort's fixed
`database.sqlite3` filename. In `.kopiur-consistent`, they publish:

- `database-<generation>.sqlite3`, a new private file for every attempt.
- `manifest.json`, with version 2, generation, exact source database name,
  artifact basename, byte length, SHA-256, `integrity_check: ok`, and UNIX
  `completed_at` timestamp.
- `attempt.json`, whose generation must equal the manifest generation and whose
  state must be `complete`.

The directory is 0700 and the files are 0600. Before inspecting the source, the
hook records a new `running` attempt. It fsyncs the completed copy and atomically
replaces and fsyncs the manifest before marking the attempt complete. Failures
leave `failed` or `running`, so a previous valid DB cannot be silently accepted
as a fresh capture. The previous referenced DB survives failed attempts. A new
attempt deletes obsolete generation files while retaining the artifact named
by the previous manifest. At most two complete generation files remain after a
successful rerun. A crashed attempt can leave an unreferenced partial file,
which is never a valid restore source and is cleaned on a later attempt.

Python uses a kernel-released flock for serialization. Node uses an exclusive
lock directory because Node core has no portable flock. Normal errors and its
internal deadline release this directory on process exit. SIGKILL or a container
crash can leave `.kopiur-consistent/lock` behind; subsequent hooks fail closed.
Only after proving no hook is executing may an operator remove that exact empty
lock directory. Never expire it by wall-clock age while a hook may still run.

For every restored media point, require all of the following before promotion:

1. The manifest is version 2 with the exact database name from the table and a
   lowercase 32-hex generation. The artifact basename must be exactly
   `database-<generation>.sqlite3`. Reject symlinks and path traversal.
2. `attempt.json` is exactly the same generation with state `complete`. A failed,
   running, missing or mismatched attempt is not a usable fresh recovery point.
3. The artifact's size and SHA-256 match the manifest, and a fresh SQLite
   `integrity_check` returns exactly `ok`.
4. For a new capture, `completed_at` falls within that hook's recorded invocation
   window, allowing only measured clock skew. Record the invocation window and
   generation with the Snapshot ID. For a historical restore, compare against
   that original capture window, not the current wall clock. A checksum-valid
   old generation is not proof of fresh backup execution. Match NAS and R2 to the
   exact same generation, source identity and replication lineage.

The Docker fixture suite executes these checks through `accept_artifact` and
proves rejection of old timestamps, mismatched attempts and altered bytes. The
capture-time upper bound, expected-generation linkage to controller evidence,
and restored filesystem symlink checks remain required operator checks.

### Media delivery and restore gates

1. Provision separate `kopiur-kometa` and `kopiur-audiobookshelf` vault items with
   independent `NAS_KOPIA_PASSWORD` and `R2_KOPIA_PASSWORD` fields. App-local
   ExternalSecrets render the NAS config and existing R2 transport credentials.
   Local tests do not provision or verify secrets.
2. Deliver the separate deadline prerequisite before the app manifests. Include
   both media policy names and all four repository names in config, snapshot,
   verify, snapshot-replication and maintenance job selectors. Prove admission
   and real Job `activeDeadlineSeconds: 3600`; a hook timeout is not a mover
   timeout. The application patch deliberately does not edit Kyverno policies.
3. Under the authorized delivery workflow, require exact-revision Flux health,
   synced ExternalSecrets, Ready repositories/policies/schedules, a fresh NAS
   snapshot and its exact successful R2 replica. Verify all real mover identities
   match 568 and drop ALL. `runOnCreate: false` avoids implicit initial captures.
4. Independently restore that NAS point and its matching R2 point to separate
   disposable PVCs at the capacities above. Use non-root Restore options
   `skipOwners: true`, `skipPermissions: true`, `skipTimes: true`. Verify private
   file bytes survive and app UID 568 can read all required state. Do not infer
   metadata-replay support from a successful non-root byte restore.
5. Validate the version-2 contract before moving the raw DB and its WAL, SHM and
   journal aside together. Promote only the verified copy to its exact source
   path in each disposable restore. Set UID/GID 568, private readable modes and
   fsync the file and parent directory. Never promote into production.
6. Compare table names and aggregate row counts between the two promoted DBs,
   plus required non-DB checksums. For Audiobookshelf include the full metadata
   tree. For Kometa include YAMLs and UUID. Do not publish credentials, account
   names, library titles, configuration values or records as drill evidence.
7. Install and verify deny-all ingress/egress before boot. Use exact production
   image digests and security contexts, no host networking, Service selectors or
   public routes, and no production NFS. Mount empty disposable `/audiobooks`
   for Audiobookshelf and the restored PVC `metadata` subpath at `/metadata`.
   Omit Kometa's config-copy init container so it cannot overwrite restored YAML.
   Supply required credential structure privately and keep integration endpoints
   unreachable. Disable scanning, ingestion and automation in disposable config
   where supported. Network denial remains mandatory.
8. Kometa must parse the restored YAML/UUID/cache and start its real process into
   the idle scheduler. Never run collections or overlays against production Plex.
   If offline startup needs upstream connectivity, record that unmet boot gate
   rather than accepting syntax checks as boot evidence. Audiobookshelf must
   pass `/healthcheck` on port 80 and demonstrate restored account, library and
   settings state, not an empty installation or just HTTP 200.
9. Keep VolSync and existing recovery points until both independent restores,
   equality checks and application checks pass. Retain accepted NAS/R2 points;
   do not call an R2 replica pinned without controller proof. Verify unchanged
   production health and VolSync protection. Remove only owned disposable drill
   resources through the parent's authorized workflow.

### Local media verification

Pull the exact image references declared in the two HelmReleases, then run:

```sh
python3 -m unittest discover -s scripts/tests -p test_kopiur_media_sqlite.py -v
scripts/validate-app --offline media/kometa
scripts/validate-app --offline media/audiobookshelf
```

The suite requires locally available images and fails rather than skipping real
SQLite execution when Docker or an image is missing. It uses disposable,
network-disabled containers with UID/GID 568, no capabilities, no-new-privileges,
a read-only image filesystem and a private tmpfs at `/config`. Only the timeout
constant changes for the deadline test; all SQLite calls execute against the
installed app driver. It checks advancing committed WAL state, unchanged source
DB/WAL bytes, integrity, hashes, file modes, bounded generation cleanup, missing
DB and companion state, symlinks, real asynchronous open and backup-step errors,
and a held EXCLUSIVE SQLite lock that reaches the hook deadline. No API mocks
substitute for the installed driver.

These are hook and manifest tests, not production backups, backend restores,
server-side CRD validation or application boot evidence. Those acceptance gates
remain separate even when all local tests and offline app validators pass.
