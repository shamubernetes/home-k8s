# Whisparr recovery

## Accepted current original

The original NAS `0b29b45c67766b496f1a561ff16892e3` and exact-lineage R2 `c25e32fda71caf7dfc6438d92cb32218` remain Retain, with original Snapshot CR UIDs `b9f9d354-4aba-4a6d-9db3-fab636ba5d52` and `16f06a4c-9f3f-4f19-9112-cda99811a02c`. The self-contained native bundle was captured online without a writer hold. It contains the complete paired application file tree, original config and PG17 native archives. Diagnostic text logs are excluded only from the stability comparison; every log byte remains archived.

Actual trusted-main [run 39505309214](https://github.com/shamubernetes/home-k8s/actions/runs/39505309214) at reviewed commit `54f4d5c23a4ae108fdc6189e4868f0ffef438550` recorded BOTH backend receipts and the final `independent_original_native_recovery=true` / `nas_r2_native_data_equal=true` receipt. Each independent restore recovered 105911 paired application files and matching native PostgreSQL data fingerprints. The main database had 43 tables and 133668 rows; logs had three tables and 24017 rows. Each native API recovered the same 28635 movie records, aggregate native identity/title/path fingerprint `1ac725eb658d730688c8d4a8fe6e9a5c67a551efd67b7b9c06d5424c43840b78`. All networked backend readers were destroyed before native application boot and the original backends were connected read-only.

The overall workflow ended cancelled and the native step ended failure, not success. Its actual data qualification completed, and final delivery then hit BrokenPipeError at `2026-10-20T23:19:28Z` after the private receiver deadline. The approved receipt-preservation change retained both completed backend results and the successful final comparison before socket delivery. Do not discard those real results, replay the consumed socket or rerun unchanged original restoration merely to obtain a green transport wrapper. No final-data qualification failure is claimed or hidden. The global executor separately accepted the required protected-scope recovery gates, not an automatic retirement flag in the diagnostic artifact.

The current production effective-key authenticated API also returned HTTP200. Existing GitOps and 1Password runtime/key references remain valid and unchanged; no new plaintext credential archive is needed. This proves the original protected config/database/file state, not a new backup of all external media or Books. Those existing mounts and NAS bytes remain untouched.

## Daily protection and retained history

Supported daily primary capture is enabled at 23:50 UTC, with offsite copy at 00:30 UTC. Only that primary lane is active; the older draft daily lane stays suspended. Both are daily, never hourly. The same already-qualified inline online helper remains delivered without a production PodTemplate change, stop, pause, freeze, restart, scale-down or writer hold. Accepted actual NAS/R2 native recovery is reused without a redundant controller-restore or arbitrary calendar-day waiting gate.

Before retirement, exact production PVC UID `c33f4bc5-94d5-48cf-8349-949d638e61c9` was verified Bound and prune-disabled. The unchanged bound PVC/PV and existingClaim survive removal of the old Flux bootstrap declaration. Its immutable historical dataSourceRef is provenance, not a running VolSync dependency. Never delete or recreate that bound production claim to clear it.

Retirement removes only the two replaced ReplicationSources and old restore destination/bootstrap. Original NAS/R2 ExternalSecret names, target Secret identities and provider references remain local to this app so all legacy Restic history stays accessible. Native original points and all repository history remain retained, with no offsite pruning.

## Independent recovery

Use the supported original reader workflow with exact retained NAS/R2 identities and fresh independently bound private transport only if a new qualification is necessary. Never reuse consumed markers or sockets. Restore the chosen self-contained complete bundle into isolated storage, verify checksums and native DB/file data, remove networked readers, and boot the pinned application against a disposable PG17 instance with no production network. Reuse the persisted GitOps/1Password runtime sources for deliberate recovery adoption, never a restore over production.
