# TubeArchivist database recovery contract

TubeArchivist is not a disposable cache. Recovery requires SQLite `/cache/db.sqlite3`, Elasticsearch application indices, relevant cache files, and the separately protected NFS videos. Redis DB 15 is recreated empty, not restored from shared Dragonfly. This change leaves VolSync and existing Elasticsearch SLM untouched. Kopiur policy, schedule and replication are suspended pending the delivery gates below.

## Capture

`app/backup.py capture /cache/kopiur/current` runs inside the pinned TubeArchivist 0.5.12 app with its existing ES credentials. It performs SQLite's online backup API, checks integrity and required Django tables, then invokes the shipped `appsettings.src.backup.ElasticBackup` exporter. The native NDJSON ZIP is an actual portable ES data artifact on the application PVC, not a reference to the ES-local snapshot repository.

The exporter is wrapped to bound HTTP calls and reject HTTP failures, partial shard results and search timeouts. Capture records mappings/settings, verifies the seven-index allowlist, unique document IDs and before/export/after counts, hashes the artifacts, fsyncs, and publishes one completed directory. It has an exclusive lock and a 600-second deadline. No partial or stale current artifact can make a failed hook successful.

The bundle includes:

- `db.sqlite3`, checked native SQLite backup of users, auth state and schedules.
- `elasticsearch.zip`, the app's native JSON backup for `ta_channel`, `ta_video`, `ta_download`, `ta_playlist`, `ta_subtitle`, `ta_comment`, `ta_config`.
- `indices.json`, per-index mappings/settings needed for native ES recreation.
- `manifest.json`, SHA-256 checksums, document counts and ID/source digests, SQLite table counts, versions, start/end time and consistency limits.

This is a bounded per-index PIT export plus an online SQLite copy, not a distributed transaction across ES, SQLite, files and Redis. Count drift fails capture, but updates that preserve count can still span different instants. Avoid index-schema changes during the window. For an application-wide point-in-time promise, an approved quiesce mechanism would be an additional prerequisite. The routine contract promises the documented bounded recovery window, not that stronger guarantee.

The existing `ta_snapshot` repository is inside the shared ES data PVC and includes security state in historical snapshots. It is not used here. Capture does not register snapshot repositories, change SLM, create ES snapshots, export other indices or read cluster credentials. Native JSON export uses PIT/search calls only against the app index set.

## Independent NAS and R2 restore

1. Select the exact snapshot ID for `tubearchivist@media:/pvc/tubearchivist` on the desired repository. Generate a Restore for a newly named PVC, never the live claim:

   ```sh
   python3 scripts/k8s92-mixed-db-restore-manifest.py tubearchivist nas-smb "$SNAPSHOT_ID"
   python3 scripts/k8s92-mixed-db-restore-manifest.py tubearchivist r2 "$R2_SNAPSHOT_ID"
   ```

   These commands print resources only. The delivery lane owns approval, application and read-back verification. Run both backends separately, retain each actual snapshot ID and verify a successful nonempty Restore, not just a successful file-list command.

2. Keep the restored PVC immutable. Export its root read-only for isolated validation on the approved native remote runner, then run:

   ```sh
   python3 scripts/k8s92-mixed-db-boot.py tubearchivist "$RESTORED_PVC_ROOT"
   ```

   This creates a separate exact-version ES 8.19.22 server called `k8s92-ta-restore` and brand-new Redis. It verifies SQLite and ZIP inventories, rejects a nonempty ES target, creates only the seven application indices from mappings, uses native ES bulk import, then reads every restored ID/source back and compares its digest. No global cluster settings, security indices, API keys, users, roles, unrelated indices, ILM or production SLM policy are imported. The fixture's ES security is disabled only on the isolated, unpublished internal Docker network, never on production.

3. The validator copies the restored cache to a working directory, replaces raw `db.sqlite3` with the validated online artifact, removes old WAL/SHM/journal files, disables periodic tasks on that copy, and boots the pinned application. It checks `/api/health/` with a valid Host header. Fresh Redis DB 15 must have DBSIZE 0 before boot. Containers are removed on completion; raw logs stay private in the agent scratch directory.

4. Check restored users, subscriptions, playlists, download records and video metadata against the recorded baseline. Video playback and filesystem reconciliation additionally require a separately recovered NFS library. Do not expose the restore externally or re-enable schedules/integrations before review. Decide which abandoned download/index jobs to requeue. Redis-held transient jobs, sessions and progress are intentionally discarded. SQLite auth records remain recoverable, but a DR test must not leak their tokens.

In an approved separate Kubernetes DR environment, use the same `backup.py` tool with fresh ES authentication and a target whose `cluster.name` is `k8s92-ta-restore`:

```sh
python /backup-contract/backup.py verify /restored/kopiur/current
python /backup-contract/backup.py restore-es /restored/kopiur/current
```

The ES endpoint comes from `ES_URL`; use new credentials via the normal protected environment or `ELASTIC_PASSWORD_FILE`. Never point restore at `elasticsearch.database.svc.cluster.local`. Require a fresh ES target with no hidden/security indices, no shared Dragonfly reuse, and no production integrations. Replace SQLite on a working cache before starting the app, as the boot validator does. The script deliberately refuses any different cluster name or a nonempty ES target.

## Delivery prerequisites and tests

Run `python3 scripts/k8s92-mixed-db-test.py --app tubearchivist` on the approved native remote runner for a fixture using the pinned application, ES and a disposable Redis instance. The source containers are removed before JSON/SQLite recovery. Counts and every exported ID/source are checked after native ES import; app boot is an additional check.

The application already ships its JSON exporter, Python, requests and SQLite support. This route does not require a production ES `path.repo` change, S3 plugin, repository migration, shared ES storage edit, CNPG change or shared Redis backup. The exact remaining prerequisites are parent-provisioned credentials for the declared app-scoped NAS/R2 repositories, sufficient writable app-PVC staging space, a capture duration within 600 seconds at live scale, source credential permission for existing index metadata/count/PIT/search calls, qualified private-file mover permissions, and independent approved NAS/R2 restore drills. Do not extend database privileges speculatively.

No production capture, ES snapshot API, SLM modification or backend acceptance was executed in this lane. Config readiness and a successful synthetic fixture alone do not satisfy these remaining gates.

## Repository credentials and activation

`app/kopiur-repositories.yaml` declares the dedicated NAS SMB and R2 repositories.
`app/externalsecret-kopiur.yaml` reads only `kopiur-tubearchivist` from the existing
`op-secret-store`. The parent must provision this item with independently approved
`NAS_KOPIA_PASSWORD`, `NAS_RCLONE_CONFIG`, `R2_KOPIA_PASSWORD`,
`R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` fields. The rclone configuration must
define the `mnemosyne` SMB remote with the approved app-specific identity. No new
reference to `volsync-template`, another app's item or guest SMB access is added.
The existing runtime database credentials and service endpoints are unchanged.

Repository creation and health probes run when deployed with valid credentials.
Maintenance is disabled. Snapshot policy, snapshot schedule and replication stay
suspended. Before deployment, qualify the mover deadline policy for these exact
repository names; before capture, qualify the hook RBAC and root/private-file
mover opt-in. Those shared security changes are separate from this application PR.
See `k8s92-mixed-databases-handoff.md` for remote-only commands and all remaining gates.
