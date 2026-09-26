# K8S-92 mixed-database deployment candidate

## Scope

This candidate adds repository wiring and bounded database capture for Grimmory
and TubeArchivist. It is not an accepted backup migration. VolSync, its credentials,
PVCs, shared database services and Elasticsearch SLM/history remain unchanged.
All snapshot policies, schedules and replication remain suspended. Repository
maintenance is disabled. Repository creation and health probes are enabled, so
merging this PR is a live-affecting deployment even before capture is activated.

The publication pass started at `12ce842cbc4a53f884674a3ca865e93b53f38132` after
`git -c pull.rebase=false pull --ff-only` preserved the previous worker's edits.
It performs source review, edits and GitHub orchestration only. No application
test, container, repository validator or workload is run locally. File editing
tools perform their built-in syntax checks; those are not runtime evidence.
No live mutation, secret access, credential provisioning, merge or tracker edit
belongs to this candidate pass.

## Repository and credential contract

Each app declares `<app>-nas-smb` and `<app>-r2` Repository resources, plus
`<app>-kopiur-nas` and `<app>-kopiur-r2` ExternalSecrets. App wrappers depend on
Kopiur, snapshot-controller, Rook, VolSync and external-secrets-secret-store.
Health checks cover the new ExternalSecrets and repositories; suspension and
replication status require explicit read-back, not a Ready inference.

The parent provisions separate `kopiur-grimmory` and `kopiur-tubearchivist` items
through the existing credential service in the Kubernetes vault. Each must have:

| Field | Purpose |
| --- | --- |
| `NAS_KOPIA_PASSWORD` | Unique NAS repository encryption password |
| `NAS_RCLONE_CONFIG` | Complete rclone configuration defining `mnemosyne`, using the approved app-specific SMB identity |
| `R2_KOPIA_PASSWORD` | Unique R2 repository encryption password |
| `R2_ACCESS_KEY_ID` | Independently approved app-scoped R2 access key |
| `R2_SECRET_ACCESS_KEY` | Matching R2 secret key |

NAS paths are `mnemosyne:Backups/kopiur/grimmory-smb` and
`mnemosyne:Backups/kopiur/tubearchivist-smb`. R2 uses the established
`storage-backup` bucket and respective `kopiur/grimmory/r2/` and
`kopiur/tubearchivist/r2/` prefixes. A separate prefix is not an authorization
boundary. The parent must verify the provider's actual credential scope and
obtain approval for any broader backend access before deployment. Do not reuse
`volsync-template`, another app's credentials or guest SMB access for this lane.
No credential values belong in Git, PR text or CI.

Capture continues using each application's existing in-container DB credentials
and endpoints. This candidate does not provision DB users, alter privileges,
change the credential service or rotate the live application secrets.

## Store coverage and consistency

| App | Captured and restored here | Separate dependency or deliberate exclusion |
| --- | --- | --- |
| Grimmory | Full PVC with `grimmory-data/`, a transactional MariaDB 11.8.8 logical dump including views/routines/events/triggers, checksums and manifest under `mariadb-config/kopiur/` | NFS `Books` and `Books/bookdrop` require independent recovery. Raw MariaDB physical files are retained in the full PVC but not qualified for boot. |
| TubeArchivist | Full `/cache` PVC, validated SQLite auth/schedule state, seven native ES NDJSON indices, mappings/settings, per-index ID/source digests, checksums and manifest in `kopiur/current` | NFS `TubeArchivist` videos require independent recovery. Redis DB 15 is recreated empty; transient jobs/progress/sessions are discarded. Global/security ES state, unrelated indices and historical SLM snapshots are not imported. |

The exact ES set is `ta_channel`, `ta_video`, `ta_download`, `ta_playlist`,
`ta_subtitle`, `ta_comment`, `ta_config`. Capture rejects missing or additional
`ta_*` indices, changing metadata, count drift, partial shards, HTTP failures,
duplicate IDs and corrupt artifacts. Capture has a 600-second deadline and an
exclusive lock. Grimmory rejects non-InnoDB tables, a table count below 73,
version drift and dump failures, with a 180-second deadline and exclusive lock.
Both hooks use `continueOnFailure: false`. Capture never falls back to a raw
live database copy as evidence of a successful logical export.

These are bounded online captures, not app-wide atomic checkpoints. Grimmory's
SQL snapshot is not atomic with the later filesystem snapshot. TubeArchivist
uses per-index PITs plus a separate SQLite copy. Same-count updates may still
span different instants. Avoid schema migrations and record capture start/end
and CSI timestamps. Strict cross-store point alignment requires approved
quiescence/reconciliation work; remote CI cannot supply that guarantee.

## Required gates before deployment and activation

1. Green protected checks on the exact candidate head. They cover static analysis,
   rendering and image gates, not actual mixed-database restore execution.
2. Native remote fixture execution on the existing `ghar-set-zoo` DinD platform.
   At the starting main revision, `recovery-verify.yaml` accepts only `tunarr` and
   `deadlines`. There is no mixed-database suite. A separate reviewed CI-only
   prerequisite must add it and reach main before dispatch. Do not repurpose a
   Tunarr script or dispatch another suite and claim this lane passed.
3. Parent-provisioned unique repository credentials, real repository readiness,
   and app-scoped mover deadlines before deployment. Existing media deadline
   matches cover Kometa and Audiobookshelf, not these apps. Shared Kyverno/RBAC
   changes must remain separate from this application PR.
4. Before capture, verify hook pod selectors, container names, ConfigMap mounts,
   runtime substitution, controller hook RBAC and the root/private-file mover
   authorization. The policies request root plus `DAC_READ_SEARCH`; no namespace
   opt-in is added here. Root movers must not silently bypass the existing gate.
5. Qualify the exact production LSIO capture tools and app-user dump privileges,
   ES metadata/count/PIT/search permissions, writable staging capacity and live
   export duration. The official MariaDB fixture does not qualify LSIO startup.
6. Under parent-owned maintenance and approval, deliver the exact revision,
   verify source app health, authorize a controlled capture, and complete NAS
   and direct R2 restores independently into new PVCs. Keep schedules and
   replication suspended while proving these paths. R2 proof must work with
   NAS unavailable and without NAS-restored files as input.
7. Boot each restored working copy in isolation, compare representative users,
   library/book records, subscriptions/playlists and video metadata to a source
   baseline. Separately recover NFS content before claiming full app recovery or
   playback. Preserve VolSync and history through the rollback window.

The previous worker recorded Ready app containers, MariaDB 11.8.8 with 73 InnoDB
base tables, and matching TubeArchivist exporter source. That is historical
read-only context, not refreshed production evidence or successful capture.

## Remote-native commands

Run only inside the approved credential-free native-amd64 ARC CI job after the
CI-only prerequisite is available. Checkout the exact reviewed candidate SHA,
keep checkout credentials unpersisted, use workflow-owned pinned tools outside
the candidate Mise config, and pull each digest-pinned fixture image before
starting the internal Docker networks. No production secrets are needed.

```sh
unset K8S92_COLIMA_ROOT_DISK
export KUBECONFIG=/dev/null TALOSCONFIG=/dev/null SOPS_AGE_KEY_FILE=/dev/null
for app in grimmory tubearchivist; do
  scripts/validate-app --offline "media/$app"
  python3 scripts/k8s92-mixed-db-test.py --app "$app"
done
```

Fixture images are declared as `MARIA`, `GRIMM`, `TA`, `ES`, and `REDIS` in
`scripts/k8s92-mixed-db-test.py`. Confirm every exact digest supports amd64.
Native fixtures must prove source removal, native import, object/content checks,
negative-path rejection and real app boot. Keep raw logs and exported account
fixtures private. Synthetic fixtures do not prove backend restores.

On the separately authorized remote recovery runner, generate resources for
real snapshot IDs. The generator prints resources only and never applies them:

```sh
python3 scripts/k8s92-mixed-db-restore-manifest.py "$APP" nas-smb "$NAS_SNAPSHOT_ID"
python3 scripts/k8s92-mixed-db-restore-manifest.py "$APP" r2 "$R2_SNAPSHOT_ID"
```

The parent reviews and applies each resource through its controlled delivery
path, reads back the exact target and requires a successful nonempty restore.
Never reuse the production claim or recycle one PVC for both backend proofs.
After exporting each restored PVC root to the isolated remote runner:

```sh
python3 scripts/k8s92-mixed-db-boot.py grimmory "$GRIMMORY_RESTORED_PVC_ROOT" \
  --database-user "$ORIGINAL_DB_ACCOUNT_NAME"
python3 scripts/k8s92-mixed-db-boot.py tubearchivist "$TA_RESTORED_PVC_ROOT"
```

The SQL account name preserves definers; passwords are freshly generated. The
boot helper rejects restored links/special files, publishes no ports, uses an
internal Docker network, disables TA periodic tasks on a working copy and
removes its own containers afterward. It never mounts production NFS.

## Parent read-back after authorized deployment

```sh
for app in grimmory tubearchivist; do
  kubectl -n media get externalsecrets "$app-kopiur-nas" "$app-kopiur-r2"
  kubectl -n media get repositories.kopiur.home-operations.com "$app-nas-smb" "$app-r2"
  kubectl -n media get snapshotpolicies.kopiur.home-operations.com "$app" -o yaml
  kubectl -n media get snapshotschedules.kopiur.home-operations.com "$app-daily" -o yaml
  kubectl -n media get snapshotreplications.kopiur.home-operations.com "$app-nas-to-r2" -o yaml
  kubectl -n media rollout status deployment/"$app" --timeout=5m
  kubectl -n media get pods -l "app.kubernetes.io/name=$app" -o wide
done
kubectl -n media get snapshots.kopiur.home-operations.com,restores.kopiur.home-operations.com
```

Verify the full merge revision in Flux separately. Ready status from before the
deployment is not evidence for this candidate. Activation and VolSync retirement
are not authorized by publication of this PR.
