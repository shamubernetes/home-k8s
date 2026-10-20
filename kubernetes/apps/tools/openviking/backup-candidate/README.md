# openviking daily backup candidate

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
| tools | openviking-kopiur-nas | KOPIA_PASSWORD, KOPIA_RCLONE_CONFIG | Unique NAS Kopia password and SMB identity restricted to private share kopiur-openviking |
| tools | openviking-kopiur-r2 | KOPIA_PASSWORD, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY | Independent R2 Kopia password and identity restricted to kopiur-openviking/kopiur/openviking/r2/ |

Generate passwords independently, keep original decryption keys in durable
escrow, and verify item/field metadata and resulting Secret key presence without
printing values. No `volsync-template` extraction, guest SMB identity or shared
API token is assumed. `backend-identity.json` records verified item/field metadata
from the provisioning handoff. No missing native API or DB-role Secret is invented.
The R2 endpoint follows the current Kopiur peer backend host, without a URL
scheme. Dedicated identity permissions still require real backend qualification.

The coordinator must extend all relevant shared Kopiur mover deadline selectors
for namespace `tools`, policy `openviking-native`, repositories `openviking-nas-smb` and
`openviking-r2`, and replication `openviking-native-nas-to-r2` before registration.
Cover capture, verify, replication, discovery/probe and applicable maintenance
jobs. Hook Jobs already have a 600-second deadline. Repository maintenance is
explicitly disabled pending its own qualified schedule, not backup capture.
Check Kopiur controller hook RBAC and namespace mover admission separately.
Shared policy changes are deliberately not part of this application-only PR.

Candidate capture is daily at 18:30 America/New_York,
replication daily at 20:30, quick verification daily
at 21:30, deep verification Sunday at
22:30. These are natural later-today slots only if
the coordinator qualifies and registers the candidate before its slot. Never wait
overnight or change to hourly for qualification. `runOnCreate` is false.

Rendered CSI source identities are exact PVC names, never a broad selector:
- `/pvc/openviking`
- `/pvc/openviking-auth`
- `/pvc/openviking-models`

Default point deletion is Retain; strict read-error and verification behavior is
preserved. No existing coverage is muted or retired. The coordinator still owns
real original NAS/R2 capture, lineage, native independent restore, exact-revision
rollout and any eventual VolSync retirement. Dry-run acceptance is not production
recovery qualification.

## State and native recovery

The live app mounts `openviking` at `/app/.openviking`, `openviking-auth` at
`/auth`, and Ollama mounts `openviking-models` at `/models`. The old VolSync pair
covers only `openviking`. The effective declared workspace is
`/app/.openviking/data`, not a guessed SQLite file. Its actual top-level entries
include `viking`, `vectordb`, `_system` and process lock/PID files. The outer PVC
also contains `ov.conf`. OAuth refresh state is persisted separately at
`/auth/codex_auth.json` and can be newer than the bootstrap Secret.

Installed native source has `POST /api/v1/pack/backup` and `/restore`. Backup is
an account-maintenance, restore-only OVPack, not a single URI export. The hook
executes in the existing app, calls its loopback native backup API with
`include_vectors: true`, validates using that exact installed package's native
manifest/content/dense-vector readers, and stages the package plus configuration
and refreshed OAuth state. It uses the same app's own runtime key inside that
app process, never provisions that key into a new service. There are no PodTemplate
changes or ConfigMap mounts. Kustomize copies the helper into the hook command.

All three original PVCs are additional CSI sources. This is intentional. Native
OVPack documents all public scopes of the request account, not full `_system`
identity/account internals, and is not a cluster-wide multi-account backup.
A logical package cannot silently replace those stores or OAuth refresh state.
The models are retained so an isolated boot does not require downloading models.

Additional activation/recovery gates:
- Discover actual account catalog and native per-account coverage without logging
  user content. Qualify native restore for all required accounts and private
  `_system` state, not only the default account's public scopes.
- Prove the installed online vector snapshot is supported and consistent while
  writes continue. `include_vectors: true` fails closed if native consistency
  validation rejects the package. Do not weaken it or call a raw file copy proof.
- Establish safe native export size/headroom for app `/tmp` and the data PVC.
  The helper has private directories, a nonblocking exclusive lock, stale pending
  cleanup and a 256 MiB free-space admission floor, not a proven capacity model.
- Native restore the original OVPack using temp-upload plus `/pack/restore` and
  `vector_mode=require`, recover config/auth/system state, then cold-boot the exact
  image with network egress blocked. Prove content, vectors, account isolation and
  OAuth/config provenance from independently restored NAS and R2 originals.
- Measure native-export/CSI/auth skew. These are separate captures, not an atomic
  three-volume snapshot. The bundle records this unqualified boundary explicitly.
