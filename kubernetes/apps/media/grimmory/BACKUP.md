# Grimmory database recovery contract

This adds a fail-closed Kopiur capture hook, not a replacement for VolSync. The policy, schedule and replication stay suspended until the parent delivery lane supplies app-specific credentials for the declared repositories and completes independent NAS and R2 restore acceptance. Do not retire either VolSync source from this change.

## Capture

`app/backup.sh` runs in the existing MariaDB sidecar using its application credentials, not the broken root-password login. It requires MariaDB 11.8.8 and at least 73 base tables, refuses non-InnoDB tables, and executes `mariadb-dump --single-transaction --quick --skip-lock-tables --routines --events --triggers --hex-blob --databases grimmory`. The complete process has a 180-second timeout and a lock. Authentication or object privileges must fail the capture, not silently omit objects.

Artifacts are `/config/kopiur/grimmory.sql`, its SHA-256 sidecar and `manifest.txt`. At PVC root these are under `mariadb-config/kopiur/`. A new attempt removes the old current artifacts before capture. Kopiur cannot proceed when the hook fails. The mover preserves the full PVC, including `grimmory-data/`, rather than treating the SQL dump alone as the application backup.

The SQL transaction does not make a simultaneous checkpoint with filesystem data. Avoid schema migrations during capture. Record the dump time and CSI snapshot time. Books and bookdrop are NFS data and are outside this PVC backup. The old `mariadb-config/databases/` files remain preserved as evidence, but this contract does not use them for recovery or claim that their crash recovery has been qualified.

## NAS and R2 restore

Use each backend independently. Never use NAS-restored files as proof of the R2 path.

1. Obtain the exact Kopia snapshot ID for identity `grimmory@media:/pvc/grimmory` from the chosen repository. The parent lane owns repository credentials and delivery.
2. Generate a Restore into a newly named PVC with the lane script. It prints JSON and never applies it:

   ```sh
   python3 scripts/k8s92-mixed-db-restore-manifest.py grimmory nas-smb "$SNAPSHOT_ID"
   python3 scripts/k8s92-mixed-db-restore-manifest.py grimmory r2 "$R2_SNAPSHOT_ID"
   ```

3. After explicit approval, the delivery lane applies the selected generated resource. Wait for success, inspect the actual mover results, and retain repository, snapshot ID, restored PVC and artifact checksum as evidence. A missing snapshot or mover permission error must fail acceptance. Do not point a Restore at the live `grimmory` claim.
4. Export or mount that restored PVC read-only for an isolated test. The boot validator accepts a local copy of its root and never contacts the cluster or mounts NFS:

   ```sh
   python3 scripts/k8s92-mixed-db-boot.py grimmory "$RESTORED_PVC_ROOT" --database-user "$ORIGINAL_DB_ACCOUNT_NAME"
   ```

   It initializes fresh MariaDB 11.8.8 with newly generated passwords, verifies the SQL checksum, imports into an empty `grimmory` database, compares base-table count, runs native table checks, and starts the pinned Grimmory image with restored `grimmory-data`. It checks `/api/v1/healthcheck`, then destroys only its own containers. The Docker network has no external route and no published ports. Database account name must match SQL definers; passwords do not need to match production.

5. Inspect representative library/user/book records and content paths on the restored instance as a separate operator acceptance gate. The boot validator does not mount real books or prove reading an NFS book. Keep integrations and outbound traffic blocked. Restore ownership to app UID/GID 568 on working copies before promotion.

For a separate approved Kubernetes DR environment, use the same sequence: fresh 11.8.8 MariaDB, fresh credentials, application stopped, copy the logical artifacts to a read-only mount, and execute:

```sh
K8S92_ISOLATED_RESTORE=YES bash /backup-contract/restore.sh /restored/mariadb-config/kopiur
```

`restore.sh` requires an empty target, checks the version and checksum, restores all dumped schema objects, compares the table baseline and runs `mariadb-check`. Supply it from this repository; only capture code is mounted in the normal workload. Start the pinned app only after SQL checks pass. Never import over the production sidecar and never boot the raw restored physical datadir as part of this logical procedure.

## Qualification and remaining gates

Run `python3 scripts/k8s92-mixed-db-test.py --app grimmory` only on the approved native remote runner for a disposable real-engine fixture. It creates the actual pinned application schema and fixtures for Unicode, binary values, a view, trigger, procedure and event; captures; deletes the source containers; imports into a fresh engine; checks objects and boots the restored app. It also tests non-InnoDB rejection and removal of stale artifacts.

The test engine is the official MariaDB 11.8.8 image, not the production LSIO entrypoint. Synthetic CI does not qualify LSIO capture tools, live database privileges, export duration or physical data recovery. The pinned Grimmory fixture uses bounded SerialGC settings.

Remaining delivery gates are real app-user dump privileges, actual export duration and storage headroom, root/private-file mover permissions, repository readiness, independent NAS and R2 imports and application checks, and restore ownership. Root database credentials are not a prerequisite. No live capture or backup API was invoked by this lane.

## Repository credentials and activation

`app/kopiur-repositories.yaml` declares the dedicated NAS SMB and R2 repositories.
`app/externalsecret-kopiur.yaml` reads only `kopiur-grimmory` from the existing
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
