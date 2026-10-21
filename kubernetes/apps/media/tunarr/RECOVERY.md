# Tunarr frozen rollback recovery

K8S-92 protects the retained Kubernetes PVC, not the active NAS ZooTV instance.
Keep the Kubernetes Deployment at zero replicas, its HelmRelease suspended and
its application Flux Kustomization suspended. Gamekeeper is unrelated.

## Accepted originals

Independent NAS and R2 controller restores completed before native consumption.

- NAS: `35b2e76bc0c9dd8b34946b88ed734e0c`
- R2: `2b4497cfc6ba4821de6ca47c61d03f88`
- Native image: `ghcr.io/chrisbenincasa/tunarr:1.3.15@sha256:ae8ec490459e773571b277e2f64248605c1ea64d49ee4d26aa01741409d60369`
- Equal complete original-tree fingerprint: `22bacf8f5037859db5a8349bdea373fb33dd5040fbe91444ee322c3488b8cfe3`
- Four retained SQLite databases passed native integrity and full logical-data
  inspection. Both backend results were equal. Original files were read-only;
  native startup used separate writable copies only.
- Both native processes returned HTTP 200 from `/api/version` and `/api/channels`.
  Version was 1.3.15. All nine original channel identities matched the active
  `stable-1.3/db.db` SQL catalog on each independent original.

The original harness expected every retained SQLite catalog to match, then
expected the native HTTP channel field `uuid`. Neither matches Tunarr 1.3.15.
`getDefaultDatabaseName()` selects `db.db` in the configured database directory;
`/api/channels` exposes SQL `channel.uuid` as `id`. Historical databases are
retained, not asserted to be simultaneously active catalogs.

The existing v2 consumers had already booted both actual native originals.
Read-only probes in those same consumers verified the correct `id` contract and
saved both successful native receipts before the old verifier deadline. No
transfer, original restore, or native startup was replayed. The v2 Job's stale
verifier result is a harness defect, not a claimed green Job. The source now
uses the actual API contract for future drills.

Decisive evidence is also recorded in Kaneo K8S-92. Supplementary current-attempt
receipt: `cli8623-tunarr-existing-native-proofs.json`. Existing accepted original
and legacy repository histories remain retained.

## Daily protection

The rollback policy reads the original retained PVC through a fresh CSI point.
The primary schedule is daily at 02:44 America/New_York, and offsite copy is daily
at 03:43. This does not enable the retained application or operate the NAS app.
The original PVC is declared separately with Flux pruning disabled before legacy
source removal. Source retirement requires observed enabled daily policy and
schedule, exact-revision convergence, unchanged bound PVC/PV identity, unchanged
suspended application state, and preserved legacy repository Secret identity.
