# Search consumer generation admission and watchdog review

Kaneo `i894pxqe3jf5oc43vgh6y616`, original search execution `t_427cf088`.
This is a review candidate, not deployed production recovery.

## Read-only receipt admission

Run `python3 scripts/kopiur_consumer_generation.py --ledger <original-inventory.json> --receipt <held-receipt.json>` on private original capture/restore metadata.
The helper reuses the inventory graph and `generation_lineage` rather than
creating another physical search capture. It refuses omitted shared-store
consumers, missing or duplicate sources, unresolved dependencies, mixed
capture generations, revoked or incomplete captures, unheld consumers,
automatic replay, and mismatched original NAS/R2 point IDs or manifest hashes.

Input schema is `k8s92-consumer-generation/v1`. Top-level fields are
`generation`, `capture_state` equal to `closed`, `revoked` equal to false,
`stores`, and `consumers`. Each store has `source`, `generation`, `nas`, and
`r2`, using the existing shared lineage point fields. Each consumer has
`application`, `generation`, `execution` equal to `HELD`, `automatic_replay`
equal to false, and `points`. Points add `application` to the corresponding
original physical store receipt. Every connected shared-store consumer must
appear, with all its declared dependencies. Store capture appears once even
when several applications depend on it.

These are assertions supplied by the caller, not independently observed
writer fences, immutable checkpoints, authenticated provenance, or application
behavior. Output always keeps production recovery acceptance and release
authorization false. The helper cannot establish that an application actually
remained held, nor promote a revoked journal from a stale receipt. Production
integration must read the authoritative journal under the same ownership lock
and bind observations to its closed generation. Never use this helper alone
as permission to start workers or retire points.

The test receipts are explicitly synthetic. Their dependency graph exercises
shared-store closure, not a new production inventory or acceptance lane.

## Existing consumer contracts

The preserved TubeArchivist branch has application-local `coordination.py`,
`backup.py`, and `recovery.py`. Its native drain precedes collection. Its
watchdog revokes a generation before stopping the collector. Its recovery
entry point holds native startup without worker, beat, web API, queue cleanup,
partial-download deletion, or automatic task replay. Original cache/media,
SQLite, Redis, and search points must be bound to the same closed generation.
Do not replace this with an Elasticsearch settings flag alone.

K8S-92's reconciled inventory already includes `services/zoo-cowbell` as a
relational application. Recorded live search-client observations include its
application and worker. They remain in the coherent capture cohort without
creating a second search acceptance lane or modifying a product repository.
Their client drain, durable admission boundary, and PostgreSQL/search
reconciliation are not proved by the standalone Elasticsearch fixture.

## Production watchdog design requiring review

Before any production fencing, review this ordering against the original
consumer contracts and deployment topology:

1. Discover and bind the exact consumer cohort and original resource identities
   from the authoritative inventory and live read-only observations. Refuse
   unknown writers, missing adapters, or a changed resource identity.
2. Acquire one exclusive durable generation owner. Persist intent, resource
   identities, prior admission state, and a bounded recovery deadline before
   mutation. A process ID alone is not ownership across restarts.
3. Close each application's native admission and boundedly drain admitted
   writers and broker reservations. Do not kill active application writes or
   freeze a process midway through a cross-store transaction. If a consumer
   cannot drain, revoke the generation and restore prior admission.
4. A separate restart-capable watchdog owns recovery. It reads the same durable
   journal and ownership epoch. Before stopping a timed-out collector, durably
   revoke its generation. Collector publication checks that epoch and revocation
   under the ownership lock, so a late collector cannot publish a usable point.
5. Once every consumer is drained, collect original application-local state and
   one native shared search snapshot. Bind all source hashes and NAS point IDs
   to one generation. Copy only a fresh NAS restore into R2 and bind each exact
   destination point to its original NAS point. Never recapture live state for R2.
6. Publish a closed immutable generation only after all source manifests and
   journal state agree. Reopen original admissions idempotently with verified
   acknowledgements. A failure to resume remains a recovery incident, not an
   accepted generation. Never remove unrelated pre-existing writer blocks.
7. Restore in isolation with execution held. Compare original key/security,
   runtime/config and application-visible state, including interrupted task
   intent and cross-store references. Run receipt admission against the locked
   original journal, then require explicit application release decisions.

A deployed watchdog still needs independent restart and ownership-loss tests,
including lost acknowledgements, consumer drain failure, collector timeout,
late publication, watchdog restart, failed resume and changed resource identity.
The existing disposable `FixtureFence` does not implement this production
protocol. Numeric policy approval gates production-policy acceptance, not this
safe design work. Required checks, schedules, deployment/rollback verification,
and separately reviewed incumbent retirement remain open.
