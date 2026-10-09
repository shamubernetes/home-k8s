# Search consumer generation admission and watchdog review

Kaneo `i894pxqe3jf5oc43vgh6y616`, original search execution `t_427cf088`.
This is a review candidate, not deployed production recovery.

## Isolated journal integration

`scripts/kopiur_consumer_journal.py` adds a UUID-fixture-only locked journal.
The same exclusive lock guards ownership epochs, persisted prior admissions,
native adapter observations, publication and read-only receipt admission.
The closed journal retains the exact receipt, so a stale caller cannot replace
its point IDs or promote a revoked generation. Each observation binds identity,
epoch, generation, closed admission and zero active writers. Results retain
the existing false production acceptance and release authorization fields.

An independent recovery process opens the same journal after timeout. It
persists revocation before restoring admission, verifies acknowledgements and
leaves failures retryable. A new epoch rejects old collectors. Prior closed
admissions remain closed. Adapters must perform identity-checked, bounded,
idempotent native operations, including recovery after lost acknowledgements.

The ARC tests use two real isolated SQLite writer processes and native
transactional admission. A separate subprocess performs watchdog recovery and
is deliberately killed after resume but before acknowledgement. These prove
the fixture protocol, not TubeArchivist or Cowbell application recovery.
Production source names are refused. This is not a deployed watchdog, and the
existing metadata-only CLI does not gain native observations automatically.
Before any resume, recovery preflights every native identity, admission and
epoch/generation token. A foreign hold anywhere in the cohort prevents reopening
its first consumer. Cleared tokens require the recorded prior admission, allowing
never-applied intent and lost resume acknowledgements. Each native mutation must
still atomically check ownership; this preflight alone is not a native fencing
protocol. Tests cover a later consumer with a foreign epoch or generation and a
cleared token with changed admission.

Production adapter integration, bounded collector termination and deployment
restart supervision still require review and implementation. Do not hold the
journal lock across unbounded application operations.

## Native fencing prerequisite

The isolated consumer journal now uses schema v2. Every write compares the full
expected snapshot under its existing exclusive lock before replacing any bytes.
Accepted writes advance a monotonic state revision and record a new operation
identity; malformed candidates and stale same-phase or revoked snapshots leave
the journal unchanged. Initial creation compares absence. Schema v1 journals
are refused without rewriting them, since these are disposable fixture states,
not a production migration. This is a CAS prerequisite only. It deliberately
does not shorten locks, make revocation independent of hung native I/O, change
commit/release semantics, or establish supervised command cessation.

`scripts/kopiur_consumer_fencing.py` implements a separate, UUID-source-only
SQLite fixture adapter. It is intentionally not connected to the existing
consumer journal. Under one native transaction, commands compare resource
identity, predecessor revision/operation/stage, epoch/generation and admission.
The caller must persist the exact plan before dispatch. Holds and terminal
fences use distinct operation identities and predetermined revisions. Terminal
retries reuse the same barrier; a fence retry after release never recloses
admission. Cleared next-generation tokens retain revision/operation tombstones,
and delayed older commands cannot overwrite newer ownership. Resume preserves
the recorded prior admission, including a prior closed boundary.

Native holds and direct terminal fences now persist the complete canonical plan,
including prior admission and both operation identities. Every owned retry and
resume compares that plan, so a changed prior admission cannot reopen a boundary
that was originally closed. Preparation persists a released tombstone with an
explicit prepared acknowledgement. Repeated preparation and exact terminal
retries preserve cleared tokens and admission until newer ownership advances
the revision. This is a disposable fixture schema change, not a migration of
production state. ARC regressions cover altered admission and plans, lost
preparation acknowledgements, and stale commands after newer ownership.

Five ARC regressions exercise terminal retry after release, never-applied hold
recovery followed by a delayed command against newer ownership, foreign tokens
at equal predecessor/held/terminal revisions, and exact terminal resume with
prior closed admission. This prerequisite does not resolve the existing
context-long journal lock, collector cessation, successful receipt release,
restart supervision or immutable per-generation history. Do not shorten the
journal locks or admit production adapters until those contracts are implemented
and independently exercised. Complete cohort terminal fencing and affirmative
command cessation must precede the first admission restoration.

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

## Source-bound restore preparation

`scripts/kopiur_elasticsearch_restore.py` validates the entire supplied
escrow manifest and each component before the fixture adapter creates a target.
Runtime image/version must match the source binding. Every captured environment
variable needs an explicit unchanged replay decision or a reason for omission.
Preparation refuses bootstrap password replay and configuration-root overrides.
It preserves original credential, keystore and configuration bytes.

Native restore requires exact repository, snapshot, location and application
index names. Wildcards and hidden index selectors are refused. Security uses
the native security feature state; global state remains included. The selected
repository location must equal the captured runtime's replayed `path.repo`.

The supplied authoritative ledger must identify every search consumer for that
backend. Both directions of each application/store dependency must agree, and
all ledger IDs must be unique, including stores outside the selected backend.
Explicit per-consumer index names must cover the complete restore selection.
Two consumers may share an index, but neither consumer may be omitted. The
receipt hashes the source binding, supplied ledger and exact consumer records.
This binds declarations, not their authority or a fresh source catalog.

The engine fixture supplies only its synthetic reader ledger. Unit cases use
original consumer identities with synthetic catalogs to reject missing
TubeArchivist/Cowbell coverage. Neither is the original production ledger or
an application-level recovery result. Preparation receipts keep consumer query
verification and production acceptance false. Real fixture engine checks remain
a separate gate. Original capture authority, consumer drain, catalog selection,
queries, cross-store reconciliation and release decisions remain required.

## Source-backed native index expansion

`scripts/kopiur_elasticsearch_resolution.py` expands explicitly supplied exact
or terminal-prefix selectors through authenticated loopback `_resolve/index`
reads. It checks the complete reciprocal consumer/store roster before I/O,
requires two identical fresh expansions, and refuses foreign, duplicate, closed,
hidden, empty, alias-resolved or data-stream indices. Optional native per-index
alias metadata does not change the selected physical index names.

Each consumer contract binds an immutable release image, source revision,
effective selector digest and process lifetime. The independent caller guard
must qualify those observations, plus the original engine guard, around every
read. Hashes and deployment defaults alone do not establish that evidence.
Exact selectors require explicit generation/state coverage. TubeArchivist's
`ta_*` expansion and Cowbell's media and state indices remain separate contracts.

`PreparedSnapshotExport.export_resolved_consumers` refuses a prepared snapshot
that omits any expanded index. It repeats resolution before and after native,
configuration and encryption phases. Checkpoints validate consumer authority
before credential/authentication I/O, including revocation after initial capture.
The isolated engine fixture exercises the concrete API on its own synthetic
reader and retains false production acceptance. Original effective-process
selection, coherent fencing, application queries and native production recovery
remain required.

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
