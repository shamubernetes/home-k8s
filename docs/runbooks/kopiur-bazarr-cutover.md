# Bazarr Kopiur cutover

This change extracts Bazarr from PR5424 without deploying its other four apps or changing any incumbent repositories. PR5563 and all published recovery history remain untouched.

The capture sidecar uses the existing dedicated `kopiur_bazarr` identity provisioned by `arrs/kopiur-backup-identities`. It never receives application or database-administration secrets. Its mounts exclude NFS media, add-ons and scripts. Capture pairs one native PostgreSQL transaction with a complete `/config` archive only when the file-tree inventory stays unchanged. A busy tree fails closed without stopping the app. Bazarr restores must have the format-2 paired file archive; a legacy config-only bundle is rejected before any container starts.

Automatic capture, schedules and replication start suspended. A reviewed one-off `Snapshot` can still execute the recipe while its automatic paths remain paused. The successful `bazarr-k8s92-original-retry2` request preserves the NAS point with `Retain` and inherits the established arrs mover deadline. Its NAS ID is `9a623d4c77684a42b970984ce9df7076`. The one requested replication copied it to R2 ID `c889e51bdf7ce94ee2f6b555e546842d` with zero failed or pruned snapshots. The temporary replication is paused after that pass and the copied catalog entry is retained. The two earlier failed requests produced no remote snapshot ID; their recorded OOM receipts and Git history remain available after request cleanup. The original NAS and R2 restores target separate, newly created 2Gi PVCs, never the live Bazarr PVC. These filesystem restores are prerequisites, not native/application recovery acceptance. Repository initialization and health checks use the previously qualified app-specific SMB share and R2 bucket. No production backup point is accepted by the native synthetic fixture alone.

## Activation gates

1. Verify dedicated ExternalSecrets, existing read-only role authority, controller exec RBAC, mover admission deadlines and UID568 staging access.
2. Obtain a production capture with complete database/config/file checksums, original PVC/CSI identities and capture timestamps. Preserve its exact original snapshot ID.
3. Independently restore that generation from NAS and then R2 to disposable isolated native PostgreSQL and the pinned Bazarr image. Verify all paired files, native data and representative application API reads. No restored app may contact production services.
4. Verify ordinary Flux convergence to the full merge SHA and the production API. Enable recurring capture and replication only after accepted recovery evidence.

The existing VolSync NAS/R2 configuration, historical recovery points, CNPG protection and source PVC remain unchanged. Do not retire any of them in this PR.

The shared NFS Media store remains an explicit original-ledger dependency. Database/config/PVC recovery does not prove bulk media or subtitle recovery. Its capture/restore evidence must be supplied separately before whole-state acceptance or destructive retirement. Existing 1Password runtime-secret references remain the key escrow authority; exact configuration and credential version qualification remains part of original recovery acceptance.
