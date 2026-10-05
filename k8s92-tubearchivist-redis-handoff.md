# K8S-92 TubeArchivist Redis recovery

## Observed ownership and persistence

Read-only production observations on 2026-10-05, not backup acceptance:

- TubeArchivist v0.5.12 points at `dragonfly.database.svc.cluster.local:6379/15`.
  The healthy source pod was `tubearchivist-55dd7447c5-69s46` on `k8s-oceanus`.
- Dragonfly uses the v2.0.0 digest ending `f474b3631233`, three operator-managed
  replicas, two proactor threads, 512 MiB maxmemory, emulated cluster mode,
  `lock_on_hashtags` and `allow-undeclared-keys` Lua flags.
- The actual StatefulSet has no volume claims, mounts or volumes. `CONFIG GET`
  reports `dir=""`, `dbfilename="dump-{timestamp}"`, `snapshot_cron=""`.
  `INFO persistence` reported a successful snapshot, but its files have no
  persistent volume. Replication and successful in-container BGSAVE are not a
  retained backup and do not establish recovery after losing the pods.
- DB15 census observed 456 keys, including strings, hashes, lists, sets, sorted
  sets and streams. Only five matched native `ta:` or task-result prefixes.
  Unknown/broker keys must not be assumed disposable or attributed to a writer
  from their names. The archive includes every DB15 key, without prefix filtering.
  Read-only census is transient and not a coherent checkpoint.

Pinned source is `tubearchivist/tubearchivist` tag `v0.5.12`:

- `backend/common/src/ta_redis.py`: native work queues are sorted sets with
  ordered scores. `get_next()` uses destructive `ZPOPMIN`, without a durable
  claim envelope. Task results use `celery-task-meta-*`, outside `ta:`.
- `backend/task/tasks.py`: tasks initialize PENDING metadata inside execution,
  and progress records carry task identity and STOP/KILL commands. Queue intent,
  progress and result metadata are not complete replay envelopes.
- `backend/task/celery.py`: broker and result backend share `REDIS_CON`.
  No explicit late-ack setting appears in this source or application settings.
  The native fixture records real serialized and reserved Kombu messages.
- `backend/config/management/commands/ta_startup.py`: ordinary startup deletes
  native reindex queues, locks and progress, marks PENDING results FAILED with a
  new TTL, and deletes incomplete downloads. It can also publish version-check
  tasks. `/app/run.sh` then starts worker and beat immediately.
- Django's configured sessions are SQLite `django_session` records, not a
  fabricated Redis session key. The native fixture covers a real SessionStore.

SQLite, ES documents, queue intent and media must refer to one checkpoint.
Two Redis scans are mutation detection, not a writer fence. This phase does not
claim it can reconstruct a task payload already acknowledged, or a queue item
already popped before capture. The coherence lane must prove an idle all-writer
boundary or provide durable pre-execution claim evidence. Do not infer or rerun
ambiguous media/index side effects from task names.

## Restore and reconciliation contract

The immutable v2 bundle remains complete. Expired bytes remain archived.
`restore-redis` rejects the source engine, original transport endpoint, wrong DB
and non-isolated mode. Source exclusion survives an empty engine restart.
New captures bind a hash of source host/port/DB/socket, never credentials. Older
unqualified v2 captures missing that identity fail closed. It
atomically claims a NEW empty target with a marker bound to exact manifest hash,
source identity, target identity and DB15. Empty is a target creation rule, never
an empty-source exception. A source may contain all supported native types.

A killed restore can retry ONLY the same claimed bundle. Missing live records
are restored without REPLACE. Matching values and absolute expiries are retained.
Different values, expiries, extra keys or ownership fail closed. No FLUSH, blanket
merge, rewritten task status, replay or queue enqueue is performed. Expiry uses
Redis TIME, not the restore host's clock. The marker remains an execution hold.

`recovery.py` verifies the complete target and checks exact pinned source hashes
and disposable Elasticsearch name/version before any native mutation.
It runs the real `ta_startup.Command.handle()`. Only destructive cleanup,
startup task publication and timestamp replacement are intercepted. Index setup,
application configuration and normal native migrations still run. It starts no
worker, beat or write-capable API. Every repeated held startup verifies the same
records and writes an atomic/fsynced private reconciliation report outside the
bundle. Terminal, invalid, wrong-type, opaque and interrupted state is retained without
execution. There is deliberately no generic release or blind replay command.

The standalone restored-PVC helper now uses this held path too. Its success
means native held startup, not healthy production execution. A separately
reviewed task-specific release, coherent stores, blocked-egress production-spec
boot and real independent NAS/R2 restores remain integration gates. No live
entrypoint, shared Dragonfly configuration, production state or backup schedule
is changed by this phase.

## Remote qualification

Use only existing ARC `ghar-set-zoo` via the trusted main workflow:

```
gh workflow run recovery-verify.yaml --repo shamubernetes/home-k8s --ref main \
  -f suite=mixed-tubearchivist -f commit=<exact-published-candidate-sha>
```

The suite runs focused unit regressions, then pinned native Dragonfly, ES and
TubeArchivist on an internal DinD network without production credentials.
The populated fixture uses native queue/task/progress writers, actual Celery
serialization/reservation, a real SQLite session and binary Redis types. It
removes source containers, kills a partial restore, resumes/repeats restore,
restarts the app tool, validates state after two held native startups, checks
terminal/invalid/ambiguous no-replay classifications, and proves elapsed TTLs do
not resurrect. Backend restore and full application release are not inferred.

Local runtime tests, containers and repository validators are prohibited for
this execution. Editing-tool syntax checks and `git diff --check` are not runtime
qualification. Exact remote run and head evidence is recorded on Kaneo K8S-92.
