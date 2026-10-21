# PDS online backup contract

## Why not sequential SQLite backups

The pinned production image is
`ghcr.io/bluesky-social/pds@sha256:74c650cd620eee2e6e0272b4cb76036a0271523b1742f61f813b79dc5a8b5273`.
Its OCI revision is `ed2dcb8616cd56ceab8e4f34973d2b2c67ea1bd9`,
image version `0.4.5037`, bundled `@atproto/pds` version `0.5.37`.
The anonymous registry config and exported image files were inspected, without
running the application or reading production data or secrets.

Exact upstream package evidence, also present in that image:

- [db/db.js](https://unpkg.com/@atproto/pds@0.5.37/dist/db/db.js),
  lines 14-33 and 35-60, opens individual SQLite databases, configures WAL and
  transacts on one database connection.
- [config/config.js](https://unpkg.com/@atproto/pds@0.5.37/dist/config/config.js),
  lines 33-44 and 47-79, configures separate account, sequencer, DID cache,
  actor databases and disk or S3 blob stores.
- [createRecord.js](https://unpkg.com/@atproto/pds@0.5.37/dist/api/com/atproto/repo/createRecord.js),
  lines 73-98, sequences a commit inside the actor callback and updates the
  account root separately afterward. This is not a single SQLite transaction
  spanning all three stores.
- [disk-blobstore.js](https://unpkg.com/@atproto/pds@0.5.37/dist/disk-blobstore.js),
  lines 63-81, writes blob files separately, and lines 83-134 rename and delete
  blobs independently of SQLite. The blob tree is not append-only.

SQLite's [online backup API](https://sqlite.org/backup.html) supplies a consistent
copy of one database. It does not turn those independent databases and blob
files into a whole-PDS transaction. No supported whole-store online logical
backup operation was found in this pinned runtime. Do not claim that sequential
`.backup()` calls plus a live tar provide application-consistent recovery.

## Implemented no-pause alternative

Keep the existing `copyMethod: Snapshot` and `csi-ceph-blockpool` class. CSI takes
the native **whole volume**, including SQLite databases, WAL/journals, actor
keys, disk blobs and runtime escrow. This is a crash-consistent storage point,
not an application-consistent export. The helper does not copy mutable SQLite
files, open SQLite connections, checkpoint, signal, freeze, stop or restart PDS.
Only helper execution is serialized by the existing `flock` hook.

The hook prepares runtime escrow as a private generation:

1. Validate real paths, ownership and private helper-directory permissions.
   Prepare the owned lock file without following links. Missing native paths,
   external/S3 stores, submounts, links, hard links, unreadable trees and any
   outstanding incumbent hold fail closed. `lost+found` remains the exact native
   mover exclusion. No root fallback, suppressed read errors or hold release.
2. Remove old success and fsync the directory before generation preflight.
3. Write and fsync `pending/runtime.json`, `metadata.json` and `SHA256SUMS`.
   Escrow contains effective PDS variables, including image defaults inherited
   by workload exec, plus logging/timezone settings. Values never reach logs.
4. Fsync the pending directory, rename current to previous and pending to
   current, then fsync the parent. Publish and fsync COMPLETE last.
5. COMPLETE is format-2 JSON binding the generation and manifest SHA-256.
   Metadata binds the same generation, format and explicit crash-consistency
   contract. `node /kopiur/capture.cjs verify` rejects mismatches and legacy
   markers. Its success verifies escrow only, not a completed CSI snapshot or
   database integrity. Capture output explicitly says `snapshotCompleted:false`.

Process death before publication leaves either a verifiable paired generation
or no acceptable marker. The next serialized invocation recovers abandoned
pending/previous state. Historical NAS/R2 points and the shared legacy readers
are unchanged. The prior on-PVC generation is retained as previous until the
next capture. It is not a substitute for retained remote history.

A restored point must use **the native tree and current escrow from the same
exact Kopia manifest of the CSI clone**. Never combine an old stopped tar, live
files or another snapshot's escrow. The format-1 tar readers intentionally do
not accept this format. Restore the native tree preserving modes/UID/GID and WAL
files, verify format-2 escrow as UID 1000, load that runtime privately, then let
the native SQLite engine recover on an isolated writable restore volume.

## Hot delivery without a restart

HelmRelease and its runtime patch are byte-identical to the base revision, so
the application PodTemplate is unchanged. The stable ConfigMap remains a
projected directory, without subPath or a name/checksum change. Only this
ConfigMap gets `reloader.stakater.com/ignore: "true"`. Secret-driven reloads
remain enabled. Installed Reloader was read-only inspected in kube-system at
`sha256:00380aee220b9c6fac720fbd99ca244000c839f3b391cd82d401c156ab368c60`.
Its configured v1.4.22 supports the ignore annotation in
[flags.go](https://github.com/stakater/Reloader/blob/v1.4.22/internal/pkg/options/flags.go#L33)
and [util.go](https://github.com/stakater/Reloader/blob/v1.4.22/internal/pkg/util/util.go#L88).

The shell entrypoint remains compatible with the existing command but no longer
implements acquire/check/release. Future ordinary container startup execs the
native command directly. Hot delivery does not replace an already running
legacy supervisor in memory, but the new capture never sends it a request.
Liveness checks the real native HTTP health path, never a hold exemption.
Do not deliver during an incumbent hold. Parent must prove no outstanding
request/active file and continued native process availability.

The retained daily capture cron is `17 1 * * *`, replication `17 4 * * *`, with
unchanged jitter, concurrency and retention. The policy and capture schedule
remain suspended during actively owned K8S-92 qualification. Resume only after
format-2 original recovery passes, then collect a natural daily witness. The
historical Snapshot description remains historical.

## Remaining parent gates

- Review and publish source independently. Before live delivery, hold the owning
  maintenance scope and ensure no pending old-hook capture or incumbent hold.
- Confirm exact mounted helper hashes, supported ConfigMap ignore behavior,
  unchanged live PodTemplate, Pod UID, native process start time and restart
  count, and uninterrupted real health/API responses. No restart to load code.
- Run the exact-image Linux/non-root regression. Local Docker pull failed with
  `no space left on device`; host tests do not replace this gate.
- Inspect the full source and CSI staging clone under actual producer/mover
  UID/GID. Bind hook generation, producer Pod UID, Snapshot UID, VolumeSnapshot
  UID and mover Job UID to the exact native manifest. Require the same producer
  and effective environment through CSI readiness. An intervening replacement
  invalidates the attempt. Overlapping manual hooks may select a later complete
  generation, so read the generation from the actual clone, never hook stdout.
- Prove native recovery, all SQLite integrity checks, original account/repo/CAR,
  blob and effective runtime/key equality independently from exact NAS and R2
  manifests. Crash consistency does not prove cross-store semantic equality.
- Verify a natural daily capture and replication receipt. Keep legacy protection,
  all retained history and rollback paths until these independent gates pass.

Helper delivery, a successful CSI capture, original native recovery and recurring
protection are separate gates. No stage implies the next one passed.
