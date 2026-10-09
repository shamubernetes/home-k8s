# Radarr Kopiur cutover

This change extracts only Radarr from PR5424. Bazarr is already delivered separately. It does not change Radarr's application image, PostgreSQL database, source PVC, media mounts or incumbent VolSync NAS/R2 protection. PR5424 and PR5563 keep their original branches and history.

The existing `radarr-kopiur-scoped-grants-v2` Job provisions the dedicated `kopiur_radarr` read-only identity. The exporter receives only that identity, not the application login or database administrator. Its media mount is excluded. NAS and R2 use the existing app-specific `kopiur-radarr` vault item and destinations.

The established `single-db-stable-filetree` capture hook pairs a native PostgreSQL transaction with the full config tree. It rejects any tree change during capture. Radarr logs or scheduled work can therefore reject a capture. A rejected hook must not be bypassed by falling back to the legacy config-only format. Necessary scoped downtime is approved, but any controlled interruption still needs its own verified maintenance, explicit GitOps sequence and rollback.

Capture, schedule and replication remain suspended until original production recovery evidence passes. New NAS points use `Retain`, replication has no pruning rule, and no destructive retirement is part of this change. Repository initialization and health/maintenance can run even while capture is suspended, so live deployment requires verified app-specific transport authority and mover admission first.

## Required production proof

1. Run the existing trusted `postgres-radarr` ARC suite against the independently reviewed exact candidate SHA. Its isolated native fixture is a prerequisite, not original production acceptance or evidence of online file-tree stability.
2. Verify dedicated transport credentials against the existing NAS share and R2 bucket, read-only database authority, controller exec RBAC, non-root file access and mover deadlines. Check exporter headroom against actual config and database sizes.
3. Obtain one retained original production generation. Record the original PVC/CSI identity, capture interval and exact NAS ID. A failed stable-tree attempt leaves recurring protection paused.
4. Independently restore NAS and R2 into isolated native PostgreSQL and the pinned Radarr image on ARC. Compare paired files and table content fingerprints, and verify representative movie API records. No restored app may contact production dependencies.
5. Only after original proof, enable and verify recurring capture and replication through a separate protected GitOps change. Retain the original and first scheduled recovery points.

Shared media, credential/runtime escrow, consumer recovery and safe future copied-child lifecycle remain original-ledger gates before VolSync retirement. Keep old repositories, credentials and recovery points intact.
