# Online original recovery and legacy retirement

The accepted format is online-csi-crash-consistent-pds-volume, format 2. It is not
the older stopped/tar bundle format. Independent original NAS
`c65cb96bad8cd42e089cc3734c10d6bd` and R2 `30593fcbb49b0e1ce3582d7e596f76de` were
restored to separate non-production PVCs. Native Job
`k92-pds-native-resume45716-v2`, UID `f618406e-7e03-4ed8-8ca3-b393f03c5823`,
completed on 2026-10-21 at 08:34 UTC, using original image
`ghcr.io/bluesky-social/pds:0.4@sha256:74c650cd620eee2e6e0272b4cb76036a0271523b1742f61f813b79dc5a8b5273`.
Its actual native version was 0.4.5037.

Both complete original trees, all 16 files and 1,099,972 bytes, matched
292a707e180f2acf908ff1d696c5ef47cfb6f3b3f595e14bc460e7bace34f906 and stayed unchanged.
On private writable copies, all four original SQLite databases passed WAL recovery,
integrity and full logical equality across all 37 tables and 127 rows. The format-2
COMPLETE marker, generation, runtime/metadata SHA256SUMS and same-volume CSI
contract were validated before loading the original effective runtime.

Both exact native servers recovered the original repository, complete repository
CAR, blob and authenticated original admin account. API/data fingerprint:
cb15558e72080cc72cc4f1f38e78b7aee6dab83d6276e3f9283e3b8e5569c74b.
Their installed @atproto/crypto also signed and verified the original actor key and
PLC rotation key from each original and rejected wrong messages. No original
secret values were placed in Pod environment/manifests. Native app logs were
disabled and discarded before boot. All private tests denied network and mounted
only read-only originals plus disposable writable copies, never production.

To reproduce recovery, validate the paired .kopiur-pds/COMPLETE and current
metadata/runtime/checksum files, copy the whole original root into a private
writable /pds, and use the exact original image. Let SQLite recover its captured
WAL; never open a live WAL database with immutable mode. Load runtime.json inside
Node before starting /app/index.ts, preserving original data paths and keys.
For isolated tests only, override the listening port and suppress native logging.
Node modules loaded from helper files outside /app must resolve through
createRequire('/app/package.json'), not the helper's ConfigMap directory.
No backup or recovery hold/restart protocol is required by this format.

Daily primary is 21:15 New York and offsite 22:00 New York. The actual hot-loaded
online producer and CSI source are unchanged; only automatic cadence is resumed
after this independent native acceptance. The original PVC/PV and app process are
preserved. Existing Restic credentials/access remain after legacy source/bootstrap
retirement. The active production application is never stopped or held for backup.
