# PostgreSQL plus PVC recovery lane

## Delivered scope

This candidate covers five apps with native PostgreSQL/PVC capture and wired, suspended Kopiur resources. Remote native-amd64 proof is required for every app before activation. No local tests, validators, applications or containers are permitted for this delivery:

| App | Required database | Native config | PVC |
| --- | --- | --- | --- |
| Radarr | `radarr_main` | `/config/config.xml` | `arrs/radarr`, 15Gi |
| Radarr-3D | `radarr_3d_main` | `/config/config.xml` | `arrs/radarr-3d`, 15Gi |
| Sonarr | `sonarr_main` | `/config/config.xml` | `arrs/sonarr`, 15Gi |
| Bazarr | `bazarr` | `/config/config/config.yaml` | `arrs/bazarr`, 2Gi |
| Whisparr | `whisparrv3_main`, `whisparrv3_logs` | `/config/config.xml` | `arrs/whisparr`, 15Gi |

The shared capture program is `kubernetes/components/kopiur-postgres/capture.sh`. Each app includes that ConfigMap component and app-local repositories, ExternalSecrets, policy and HelmRelease sidecar patch. VolSync resources, production database init containers, CNPG base backups and WAL archiving remain unchanged. The helper never imports the production app Secret or its superuser initialization password.

This is not live acceptance. No production capture, Kopiur restore, R2-outage recovery or production-data application boot was executed by this lane. The policies and replication are suspended. The schedules use the installed CRD's `spec.schedule.suspend`, not an unsupported top-level field. Merging these resources also rolls out a sidecar and enables repository initialization, health probes and maintenance. Provision the new Secrets and repository deadline allowlists before deployment, not merely before unsuspending capture.

## Capture and consistency

The pinned PostgreSQL 17 client sidecar shares only the app's config claim and temporary space. It does not receive the NFS media mount. The policy runs its bounded `pg_dump` hook before CSI snapshot creation and fails closed on hook failure. The application images themselves do not need PostgreSQL CLI tools.

Capture uses `postgres17-rw.database.svc.cluster.local`, not the transaction pooler. It requires a dedicated low-privilege login, rejects elevated role flags, checks PostgreSQL major version and nonempty schemas, and exports every declared database with `pg_dump --format=custom --no-owner --no-privileges`. It lists each archive using `pg_restore --list` and copies the declared config file. Archives, TOCs, config and timestamp metadata receive SHA-256 checksums. A lock serializes the exporter. The previous completion marker is invalidated before validation; a fresh marker is written only after the whole set is durable. Files are private to UID 568 with directories 0700 and files 0600.

The bundle lives under `/config/.kopiur-postgres/current`. The only accepted generation is accompanied by `/config/.kopiur-postgres/COMPLETE`. `previous` is retained through one generation for diagnosis but never accepted as the current recovery point. Plan PVC free space for the current and replacement archives as well as normal application data.

`pg_dump` supplies a transactional snapshot within each database. It does not make multiple databases or the later CSI snapshot atomic. The config bytes inside the native bundle are the restore authority. Other PVC files come from the later CSI snapshot. Activation requires explicit acceptance of config/DB skew for these PostgreSQL clients, including a separately dumped Whisparr log database. The complete exporter invocation is capped at 600 seconds and staging has a 10-minute timeout. Those timeouts do not establish a measured dump-to-CSI bound. Record the bundle start/end timestamps and CSI creation time for each accepted recovery point; reject points outside the parent-approved skew limit. Config must not change during the capture window. A settings change requires a fresh capture, not reuse of the earlier bundle. It does not extend that assumption to Chaptarr's active SQLite queue, RomM Redis/assets or Home Assistant custom components. No application pause/stop hook was added because Kopiur cleanup semantics must not be assumed to provide a finally handler.

CNPG remains the independent cluster-level recovery path. An older Barman base/WAL point must be bootstrapped into a **new isolated PostgreSQL 17 cluster**, without archiving back to the production `postgres17-v1` prefix. Export the named app databases from that recovered server, not from today's primary, when proving a historical CNPG backup. A fresh app dump does not prove the existing CNPG archive can recover.

## Prerequisites owned by the parent

For each of the five apps, populate its separate `kopiur-<app>` vault item with:

- `NAS_KOPIA_PASSWORD` and `R2_KOPIA_PASSWORD`, separately generated and escrowed.
- `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`, newly provisioned backup credentials with the narrowest provider-supported access. The manifest does not reuse `volsync-template` credentials.
- `PG_BACKUP_USER` and `PG_BACKUP_PASSWORD`, a new app-specific read-only login. Never copy the application's existing login or the CNPG superuser password into this item.

The published Bazarr candidate uses its own `kopiur-bazarr` R2 bucket and private SMB share. Its
`NAS_RCLONE_CONFIG` contains the shell-disabled SMB account, not guest access.
Transport credentials and the new `kopiur_bazarr` PostgreSQL password are escrowed
separately from application and CNPG administration credentials.

`bazarr-kopiur-grants-v1` applies the app-local `kopiur-postgres-grants.sql` through
GitOps. It reads the existing database initialization authority only in that
bounded Job, never in the capture sidecar or mover. The transaction rejects an
unowned existing role, memberships, elevated flags, unexpected schema ownership,
RLS, large objects, and existing write authority. It grants SELECT on current and
future objects owned by the app in `public`, without `pg_read_all_data`.
Rename the Job for any future input or PodTemplate change. Keep snapshot and
replication policies suspended until live grants and full-state recovery pass.

The Bazarr native ARC fixture executes this exact SQL, including idempotence,
unowned/elevated-role rejection, future-table reads, write denial with the
read-only default disabled, and denial of another database's application data.
These are synthetic prerequisite checks, not production migration acceptance.

The parent must provision and test the database grants. For each app database and its actual schema owner, the grant shape is:

```sql
-- Run only through the separately approved database-role provisioning process.
-- BACKUP_ROLE and APP_OWNER below are identifiers, not literal production names.
GRANT CONNECT ON DATABASE DATABASE_NAME TO BACKUP_ROLE;
-- Connect to DATABASE_NAME before these statements.
GRANT USAGE ON SCHEMA public TO BACKUP_ROLE;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO BACKUP_ROLE;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO BACKUP_ROLE;
ALTER DEFAULT PRIVILEGES FOR ROLE APP_OWNER IN SCHEMA public
  GRANT SELECT ON TABLES TO BACKUP_ROLE;
ALTER DEFAULT PRIVILEGES FOR ROLE APP_OWNER IN SCHEMA public
  GRANT SELECT ON SEQUENCES TO BACKUP_ROLE;
```

The login must have `NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`, no broader inherited roles, and no write grants. Review real schemas, owners, large objects, RLS and future migration ownership before accepting grants. The fixture grants cover the images' generated schemas; they are not evidence that the production role exists or has complete permissions.

The parent also owns exact Kyverno deadline allowlists for these new resource names, namespace mover/RBAC prerequisites, CNPG network access, storage capacity, credential escrow and live delivery. For each of `bazarr`, `radarr`, `radarr-3d`, `sonarr` and `whisparr` in `arrs`, the separate deadline prerequisite must cover `kopiur.home-operations.com/config` values `<app>`, `<app>-nas-smb`, `<app>-r2`; `maintenance` values `<app>-nas-smb`, `<app>-r2`; `snapshot-replication` value `<app>-nas-to-r2`; and `verify` value `<app>`. Keep the existing `snapshot-delete-batch` coverage. The current policy does not include these five app names.

The exporter and movers all use UID/GID 568 and fsGroup 568, so the helper's 0700 directories and 0600 files are readable without root. Prove that identity can also read every required existing PVC file on the CSI staging clone. No Kopiur privileged-mover namespace opt-in is needed for this non-root configuration. Do not add one speculatively; VolSync's separate opt-in is not Kopiur authorization. Confirm controller exec RBAC for `arrs` and direct writer connectivity. Bazarr's 2Gi PVC especially requires measured headroom for both current and replacement bundles. Do not modify production file ownership recursively to make a mover pass. A root-only auxiliary file or denied role is a failed acceptance gate, not permission to weaken the hook.

## Remote CI proof only

Run the commands below only on an approved remote CI runner. Local execution is prohibited for this recovery pass. Requires Docker with the pinned images, Python 3.11+, `yq`, `kustomize`, Helm and Flux. No Python third-party packages are required by the new tests or drill.

```sh
python3 -m unittest discover -s scripts/tests -p test_kopiur_postgres.py -v
for app in bazarr radarr radarr-3d sonarr whisparr; do
  scripts/validate-app --offline "arrs/$app" || exit
  scripts/kopiur-postgres-drill --fixture --app "$app" || exit
done
```

The protected `recovery-verify.yaml` currently supports only `tunarr` and `deadlines`. It cannot run this PostgreSQL suite. Add the five-app native suite through a separately reviewed CI-only prerequisite on main, then dispatch it against the exact reviewed full candidate SHA. Do not combine verifier changes with this app PR or dispatch an untrusted branch workflow. Normal PR checks render the manifests but do not execute `test_kopiur_postgres.py` or these native fixtures.

The fixture boots each pinned app against a fresh PostgreSQL 17 database. It requires real application migrations, adds a synthetic marker without replacing those schemas, stops the source app for deterministic comparison, exports using a dedicated non-superuser identity, restores into another server and compares every public-table row count. It boots the restored app, checks `/ping` and reads the marker from every restored database. It separately exercises the recovered-PVC CLI path. Invalid passwords and elevated capture identities must fail and invalidate prior success.

The deterministic fixture does not prove an online multi-store checkpoint. The unit-test fake bytes test only checksum/inventory rejection and are never passed off as native PostgreSQL evidence.

Remote Docker resources have random lane-specific names. PostgreSQL uses `--network none`; each app shares only that database's network namespace. There are no published ports, NFS mounts, host devices, production credentials or Kubernetes commands. Database/config storage is disposable tmpfs. The runner streams or copies files through Docker rather than assuming host scratch paths are visible inside the Docker daemon. It removes only resources it created.

## Live acceptance after authorized publication

1. Read back every ExternalSecret, repository and workload. Confirm all new app-specific credentials and grants exist, the exporter uses the pinned image, the existing app remains healthy, and mover namespace/deadline prerequisites are in effect.
2. After that review, unsuspend the policy for an explicit named Snapshot. Keep its schedule and replication suspended until the first capture/restore is accepted. Retain the source PVC identity, snapshot ID, capture interval, database inventory and checksums. A successful hook alone is not acceptance.
3. Restore the selected NAS-SMB snapshot to a **new disposable PVC**. Require the Restore's actual `status.phase` to be `Completed`. Export that restored PVC to a protected directory on the isolated remote runner without starting the application. Do not use the production VolSync destination or the source claim.
4. Run the following against the recovered directory. The script only reads it; all writes go to fresh disposable Docker resources. It rejects symlinks and special files, verifies the bundle, restores all named databases with `pg_restore --exit-on-error --single-transaction` as a fresh non-superuser owner, applies the paired native config to a working copy, and boots the pinned app with a loopback test DB endpoint and fresh credentials. Logs that could contain restored private data are withheld. Only counts and pass/fail metadata are printed.

```sh
scripts/kopiur-postgres-drill --verify-bundle /protected/recovered-radarr/.kopiur-postgres
scripts/kopiur-postgres-drill --restore-pvc /protected/recovered-radarr --app radarr \
  --config-mib 1024 --database-mib 4096
```

The memory-size options bound disposable tmpfs capacity, not a statement about production sizing. Increase only after checking the Docker host memory budget and measured restored data size. Run recovery drills sequentially on a constrained host. Read-only app root filesystems, no-new-privileges and dropped capabilities match production hardening.

5. Compare reported schema/data counts with the selected native recovery point and perform representative application data reads. The generic CLI does not know the user's expected library contents. Record suppressed integrations and any reconciliation differences. Do not call HTTP health alone data recovery.
6. Enable one authorized replication, verify selected snapshot identity reached R2, then perform a fresh direct-R2 restore while NAS is unavailable. Repeat bundle validation and isolated application reads. Only after both independent recoveries are accepted should the parent enable schedules and normal replication. Verification CEL is `stats.files > 0 && stats.errors == 0`; Kopia file verification does not substitute for database restore.

## Remaining apps, exact gates

| App | Current result | Required next proof |
| --- | --- | --- |
| Whisparr | Fixture runner supports its distinct environment names and both `whisparrv3_main` and `whisparrv3_logs`. Pinned image is amd64-only. Two actual local startup attempts failed inside `InvokeStub_LogManager.GetLogger`/DryIoc with `NullReferenceException`, before PostgreSQL schema creation. Disabling .NET hardware intrinsics, ReadyToRun and tiered compilation did not fix it. Suspended manifests were subsequently wired during recovery review. The local emulation failure is not a cluster consistency blocker. | Run `scripts/kopiur-postgres-drill --fixture --app whisparr` on a native amd64 Docker host with the same pinned image, or diagnose that image's clean-start failure. Both DBs, restored app startup and representative reads must pass before activating the wired resources. Do not silently omit the log DB or replace the pinned image with an unverified version. |
| Chaptarr | Audit confirms `chaptarr_main`, `chaptarr_log`, `chaptarr_cache` **and active SQLite `/config/staging.db`**, including `ingest_queue`, `import_results`, `file_tag_cache`, `staging_metadata`. No config-only policy was added. | An approved coherent capture must export all three PG databases and an integrity-checked SQLite online backup or quiesced staging DB. Define and test reconciliation of the queue versus PG state, including failure/restart cleanup. A stopped-window option requires explicit interruption approval; an online option still needs a tested SQLite-capable helper and app-level queue proof. |
| RomM | Audit confirms PostgreSQL `romm`, Dragonfly DB 12, PVC assets/resources, a ConfigMap config and an NFS ROM library. No config-only policy was added. | Decide which Redis values are durable versus rebuildable before choosing capture or a fresh cache. Establish a matching DB/assets recovery point, escrow/export the ConfigMap and needed secrets, and run an isolated boot with rescans disabled and an approved local ROM fixture. Empty current assets and unpersisted Dragonfly are not proof that asset or queue recovery is unnecessary. |
| Home Assistant | Audit confirms external PG `homeassistant`, full `/config`, Watchman SQLite and an additional `home-assistant-git` PVC. No raw-PVC-only policy was added. | A controlled native `backup/generate` test must establish the local backup agent, archive format/encryption, completion polling and custom-component coverage. Pair it with PG export and Watchman SQLite consistency, classify `/git` recovery requirements, and boot an isolated safe-mode copy without LAN access. The Supervisor-only `backup/start`/`end` methods are not available to a standalone admin token; recorder backup locking does not freeze PostgreSQL. Those production capture/credential changes are outside this lane's read-only authorization. |

Chaptarr, RomM and Home Assistant remain outside this candidate. Their audit findings are prerequisites for separate recovery designs, not reasons to publish incomplete config-only backups here. Earlier fixture reports are historical and do not replace exact-head remote CI or independent NAS/R2 recovery evidence.
