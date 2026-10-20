# adguard-home daily backup candidate

Status: implemented source candidate, not registered with Flux, not migrated.
See `INTEGRATION.md` for the tested acceleration changes and exact execution order.
Do not apply this root before its provisioning and recovery gates pass. The
existing `app/` and `ks.yaml` are unchanged. No ExternalSecret points at an
invented 1Password item. No credentials were read or provisioned in this work.

## Shared prerequisites

Dedicated backend identities were provisioned and verified by the independent
identity lane. This candidate consumes only its nonsecret metadata, uses the exact
private NAS share and dedicated R2 bucket, and includes narrowly templated backend
ExternalSecrets. It remains unregistered and ESO sync is not claimed. Native
API/DB credentials and shared admission remain separate activation gates:

| Namespace | Secret | Required keys | Scope |
|---|---|---|---|
| adguard-home | adguard-home-kopiur-nas | KOPIA_PASSWORD, KOPIA_RCLONE_CONFIG | Unique NAS Kopia password and SMB identity restricted to private share kopiur-adguard-home |
| adguard-home | adguard-home-kopiur-r2 | KOPIA_PASSWORD, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY | Independent R2 Kopia password and identity restricted to kopiur-adguard-home/kopiur/adguard-home/r2/ |

Generate passwords independently, keep original decryption keys in durable
escrow, and verify item/field metadata and resulting Secret key presence without
printing values. No `volsync-template` extraction, guest SMB identity or shared
API token is assumed. `backend-identity.json` records verified item/field metadata
from the provisioning handoff. No missing native API or DB-role Secret is invented.
The R2 endpoint follows the current Kopiur peer backend host, without a URL
scheme. Dedicated identity permissions still require real backend qualification.

The coordinator must extend all relevant shared Kopiur mover deadline selectors
for namespace `adguard-home`, policy `adguard-home-native`, repositories `adguard-home-nas-smb` and
`adguard-home-r2`, and replication `adguard-home-native-nas-to-r2` before registration.
Cover capture, verify, replication, discovery/probe and applicable maintenance
jobs. Hook Jobs already have a 600-second deadline. Repository maintenance is
explicitly disabled pending its own qualified schedule, not backup capture.
Check Kopiur controller hook RBAC and namespace mover admission separately.
Shared policy changes are deliberately not part of this application-only PR.

Candidate capture is daily at 18:00 America/New_York,
replication daily at 20:00, quick verification daily
at 21:00, deep verification Sunday at
22:00. These are natural later-today slots only if
the coordinator qualifies and registers the candidate before its slot. Never wait
overnight or change to hourly for qualification. `runOnCreate` is false.

Rendered CSI source identities are exact PVC names, never a broad selector:
- `/pvc/data-adguard-home-0`

Default point deletion is Retain; strict read-error and verification behavior is
preserved. No existing coverage is muted or retired. The coordinator still owns
real original NAS/R2 capture, lineage, native independent restore, exact-revision
rollout and any eventual VolSync retirement. Dry-run acceptance is not production
recovery qualification.

## State and native recovery

The live StatefulSet mounts `data-adguard-home-0` twice, `conf` at
`/opt/adguardhome/conf` and `work` at `/opt/adguardhome/work`. Both are required.
The work tree has `data/stats.db`, `data/sessions.db`, `data/querylog.json` and
`data/querylog.json.1`. These are not SQLite exports. The candidate captures the
whole primary filesystem with a CSI snapshot. It does not claim that an API
settings export, replica synchronization, or a copied database file proves
consistency. Runtime filters, query history, leases, sessions, TLS material and
configuration must survive native startup from the original snapshot.

There is no external database. The sync Deployment's approved runtime Secret is
`adguard-home`, generated from item `adguard-home`; do not copy that credential
into a new exporter. Replica 1 and 2 have their own PVCs and unsynchronized local
query/session state. This candidate deliberately covers the requested primary,
not three-replica history equivalence. Rebuild replicas only after proving the
primary's native config, DNS responses, filter contents and local history.

Additional activation gates:
- Verify the namespace's distinct Kopiur privileged-mover opt-in and actual mover
  read permissions for every private config/runtime file. VolSync opt-in is not
  equivalent. The root UID in this candidate does not itself grant DAC bypass.
- Restore the same original full primary to isolated same-image AdGuard without
  contacting production upstreams. Prove native database open, config equivalence,
  filter/history data and deterministic DNS queries from both NAS and R2.
- This is a crash-consistent single-volume candidate, not proven native recovery.
