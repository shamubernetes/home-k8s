# K8S-92 bounded TubeArchivist writer coordination

This is a candidate lifecycle and coherent capture contract. Remote qualification
must pass at the exact published SHA before this phase is complete. NAS/R2
acceptance, full production-spec restore boot, merge/deployment and incumbent
backup retirement belong to t_cb6c9fdd. Policies remain suspended.

## Native boundary

The pinned supervisor owns normal startup, native Celery worker, native beat,
Nginx and Uvicorn. Normal source startup retains the exact qualified v0.5.12
management commands. A restored Redis ownership marker refuses normal startup.
Resume never calls run.sh or ta_startup and never releases restored execution.

ASGI admission covers the entire native handler, including Django synchronous
thread cleanup on disconnect. Worker and beat keep shared lifecycle leases
through warm shutdown, child-process exit, broker reservation return, callbacks
and beat final SQLite schedule sync. Beat clean proof is generation-specific.
An active long request/task keeps its lease and causes a failed bounded drain,
not a force-kill or a mid-transaction snapshot. Unmanaged source processes and
DB15 clients outside the source pod veto capture. Shipped source hashes and
Django/Celery/Uvicorn/beat versions fail closed on drift.

## Bounded attempt and recovery

One permanent inode lock serializes attempts. The durable journal binds a UUID,
Linux boot ID, PID starttime and monotonic deadline. Admission closes for at most
60 seconds. The default is a 20-second drain plus a 30-second capture budget.
Native writers are drained rather than SIGSTOP-frozen midway through operations.

A detached watchdog inherits the attempt lock, never the exclusive writer lock.
It revokes staged publication, kills only the exact pidfd-qualified owned
collector/controller, and reopens admission. Process death releases kernel
leases. Native supervisor and writer admission independently recover expired or
interrupted journals, including a crash between durable REVOKED and OPEN.
Failed drain does not kill an admitted task. Closed journal state reopens on
expiry even if the controller died or its watchdog was killed. Filesystem/host
failure that prevents durable I/O is an external failure, not a claim of an
unconditional uptime guarantee.

The native supervisor resumes services when admission opens. An already warm-
shutting worker is not duplicated while its admitted task finishes. New web
requests reopen promptly. Transport/upload starts only after resume, and its
failure cannot prolong a writer hold. Every retry gets a new generation and
cannot replace a prior committed generation.

## One recoverable point

After native drain and exclusive admission, refresh all seven owned ES indices.
Reject in-flight ES writes/restores, and veto sequence-number changes. Capture
native ES ZIP, SQLite online copy and every DB15 record, including unknown,
broker, result and STOP/progress records. Veto Redis drift, SQLite file/sidecar
rewrite and cache/media filetree mutation. Absolute Redis expiry stays original.

The same hold copies required cache/partial-download files and all TubeArchivist
NFS media into private immutable tar archives. Symlinks and special files fail
closed. Raw later CSI/cache/NFS files are not accepted as the checkpoint. Export
archives, all-store manifest and generation commit are hashed and fsynced before
publication. A collector cannot declare itself coherent. Publication is
serialized against watchdog revocation. On isolated filesystem recovery, exact
file set, paths, ownership, mode and timestamps are retained and later raw
source mutation is excluded.

The suspended policy records a manifest-bound LATEST.json pointer. Restore boot
resolves only that published generation, never an unbound current/ directory.
Full media copying has an explicit bounded budget. A production library that
cannot finish within that budget fails capture rather than exceeding the hold
or excluding media. The integration lane must establish feasible production
capacity or a separately qualified coherent storage-snapshot adapter before
activation. This is not an accepted loss/skew exception.

Queued/reserved native ETA work must warm-return to the broker before capture.
Ambiguous pre-existing acknowledged/popped work remains byte-preserved under the
existing recovery.py reconciliation hold. No task status is rewritten, no
generic replay is introduced, and ordinary restored worker/beat startup remains
forbidden. Independent release/reconciliation remains an integration gate.

## Qualification

Only the existing ghar-set-zoo ARC DinD runner may execute this candidate:

    gh workflow run recovery-verify.yaml --repo shamubernetes/home-k8s --ref main \
      -f suite=mixed-tubearchivist -f commit=<exact-full-candidate-sha>

The remote suite runs the existing Redis/schema regressions, real Linux kernel
admission/journal/watchdog failure-path tests, then pinned native lifecycle and
source-loss/isolated restore tests. Unit fixture collectors are explicitly
labelled fixtures, not native proof. The native fixture runs actual task
callbacks/results, native reserved ETA return to broker, beat clean shutdown,
long-writer bounded drain failure, real populated SQLite/ES/Dragonfly plus
cache/partial/media generation equality, source removal and held startup.

Tests must report capture timeout, controller SIGKILL, collector crash, watchdog
loss with independent writer recovery, failed resume publication, failed upload,
retry, overlapping attempt refusal and missing/tampered generation rejection.
Machine evidence and precise observed timing are recorded in the phase handoff.
This source fixture is not a claimed NAS or R2 restore and does not retire
incumbent backups. No local runtime, containers or validators qualify it.
