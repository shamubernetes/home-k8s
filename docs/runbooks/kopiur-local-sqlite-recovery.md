# Kopiur local SQLite recovery

This cohort adds Kopiur protection for `arrs/cwa-bdl`, `arrs/sabnzbd`, and
`arrs/listenarr`. Keep both existing VolSync streams. A configured policy is not
an accepted recovery path until the NAS and R2 recovery drills below pass.

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
