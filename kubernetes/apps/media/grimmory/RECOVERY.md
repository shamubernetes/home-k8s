# Grimmory recovery after VolSync

The existing `media/grimmory` claim is retained outside the application render. Its live `kustomize.toolkit.fluxcd.io/prune: disabled` annotation was applied and verified before removing the legacy template. Do not delete or recreate that claim during controller retirement.

## Accepted replacement

- Independent original NAS `9ee459f876923e2b03183b270424d5cc` and exact copied R2 `e018455626361289c679709d13ac38b8`, generation `64d4a55b-04a0-48cb-8662-ccce86e1090d`.
- Native recovery run [39469907069](https://github.com/shamubernetes/home-k8s/actions/runs/39469907069) passed both backends, 73 SQL tables, all seven runtime hashes, app data and complete Books/bookdrop bytes and metadata. Native database-backed status, database restart and consumer recreation passed.
- Independent controller restores and read-only byte/owner/mode proof completed before cutover. The audited Restore definitions remain in `app/kopiur-online-restores.yaml`.
- Natural daily capture `grimmory-hourly-20261020164500` produced NAS `d0ae31c16617e754ee6cbc7d1908846c`. Natural daily replication produced R2 `0e0a0d60eb7ebc277acd953a22555746` from that exact source.
- Full file-content verification Job UID `77b90ea8-3459-4589-b9f8-afc314b7e3d7` completed, advancing `lastVerified` to `2026-10-20T18:53:54.232096971Z` with `Verified=True`.

The daily capture and replication remain at 16:45 and 17:15 UTC. Full content verification runs daily at 18:35 UTC. No capture hook stops, freezes or restarts the app. These results qualify Grimmory only, not global consistency across other Books writers.

## Retained legacy history and access

Keep these repositories and their access material. Removing runtime objects does not authorize deletion of backup data.

| Backend | Retained location | Last recorded short Restic snapshot ID |
|---|---|---|
| NAS | `${IPAM_IP_NFS}:/mnt/user/Backups/k8s/grimmory` | `51b9ab8e` |
| R2 | `s3:${R2_ENDPOINT}/storage-backup/volsync/grimmory` | `1b503407` |

The NAS repository was mounted as `/repository/grimmory` in the former mover. The NFS export and R2 bucket are shared infrastructure and remain intact.

The unchanged 1Password item `Kubernetes/volsync-template`, ID `7obukhbwm3n5zdi2gdqqjksdrq`, retains `RESTIC_PASSWORD`, `VOLSYNC_R2_ACCESS_KEY_ID` and `VOLSYNC_R2_SECRET_ACCESS_KEY`. Their presence was verified before removal. Keep the entire item, including other legacy access fields. Inject secrets privately when using Restic directly; do not recreate a controller merely to list or restore its retained repository. The endpoint and NFS address remain existing cluster variables, not credentials embedded here.

## Recovery and promotion

1. Read the current successful Snapshot and exact R2 `copiedFrom.sourceManifestId` lineage. The accepted point above remains retained; use a newer point only after verifying it.
2. Use the audited native Restore shape with fresh resource and target PVC names. Never restore over the retained production claim. Wait for actual `Completed` and current-generation `Ready=True` before publishing any consumer.
3. Validate original bytes and metadata, then the exact-image isolated native consumer. Preserve keys and runtime configuration. The existing qualification contracts document the read-only originals and disposable consumers.
4. For a fresh cluster, keep application reconciliation suspended until recovery finishes. Do not create a blank substitute claim. After validation, promote the recovered claim through GitOps, updating the application claim reference and Kopiur source together while preserving the established backup identity.
5. Verify the application route, native database, data and daily protection before ending recovery maintenance.

The retained bound production claim has UID `b0d80887-a170-43c6-a3b8-76be26fd0958`, volume `pvc-af31d069-d787-4659-b774-4208182c53e3`. Its immutable historical `dataSourceRef` names the former populator. A completed bound claim does not need that populator to keep serving data. Do not replace the volume to erase historical metadata.
