# K8S-92 shared prerequisites

Kaneo `i894pxqe3jf5oc43vgh6y616` is the durable record. Execution
`t_d69dd383` owns shared coordination, not application cutover or retirement.

## Shared interfaces

`scripts/kopiur_shared.py report --ledger <inventory.json> [--app namespace/app]`
validates the reconciled application/store graph and emits a per-application
capture plan. Every physical store appears once. Each consumer keeps its own
execution owner, native selections, escrow references, retention requirements and
remaining gates. Unknown policies stay unknown. This report cannot accept an
application recovery or authorize retirement.

The input is the existing K8S-92 inventory with `applications`, `physical_stores`
and `shared_prerequisites`. Publish the generated report in Kaneo, not the full
private live inventory in this repository. References are not secret values.

`scripts/kopiur_shared.py artifact --root <isolated-export> --manifest <manifest>`
checks exact file/tree coverage, SHA-256, size, numeric ownership and permissions.
The versioned `k8s92-artifact/v1` manifest contains `entries`, each with `path`,
`kind`, `uid`, `gid`, `mode`, plus `size` and `sha256` for files. Paths are canonical
relative paths. Directories must be listed too. Symlinks and special files fail
closed. The export must be quiescent. Native database/catalog/blob/key verification
and coordinated writer fencing remain mandatory; this helper does not implement
a production fence or validate application semantics.

`scripts/kopiur_shared.py lineage --receipt <receipt.json>` validates the same
generation across every required source and binds the NAS snapshot ID, the R2
destination snapshot ID and the original manifest hash. Input has `application`,
`required_sources` and `points`. Each point has `application`, `source`,
`generation` and `nas`/`r2` records with `snapshot_id` and `manifest_sha256`.
The R2 record also has `source_nas_id`. Missing sources, mixed generations,
wrong applications, floating point aliases and hash/mapping mismatches fail.
This is original capture lineage, not a production fence or native recovery proof.
The real transport drill uses this validator for its independently encrypted
NAS/R2 captures of the same random source generation.

`scripts/kopiur_shared.py retention --metadata <policy.json>` normalizes explicit
positive integer `rpo_seconds`, `rto_seconds`, `history_seconds`, `minimum_copies`
and `rollback_seconds`, plus an `approval_reference`. Missing values remain
unresolved. The inventory report reads an optional `retention_requirements` entry
named `approved_policy`; it never infers these numbers from legacy free text.
Supplying numbers or an unverified approval reference cannot authorize retirement.
Original points, sources and keys are always preserved by this interface.

Reports also identify native backing services that must be restored before
application boot. An unresolved dependency stays attached to its own application,
not converted into an accepted physical capture or a global lane blocker.

## Independent remote transport qualification

The reviewed existing `kopiur-identity-transport.py` harness is now a shared
interface rather than an all-services batch on an application branch. It retains
the immutable production mover image, original service-scoped NAS/R2 identities,
wrong-password denial, NAS guest denial, producer destruction, fresh-container
restoration and UUID-owned cleanup verification. Production repositories,
retained points and policies are never changed.

Dispatch the existing trusted-main `recovery-verify.yaml` workflow with
`suite=identities` and the exact reviewed branch-tip SHA. The suite runs shared
host regressions on ARC before exposing its private in-memory Unix channel.
The first operator request must be `{"operation":"select","apps":["bazarr"]}`.
The response acknowledges `selected_apps`. Subsequent requests use the existing
`{"app":"bazarr","fields":{...}}` envelope, supplied through stdin after
resolving only that service's approved vault item. No credentials may be put in
GitHub inputs, arguments, files, artifacts or logs. Resolve runner/run/pod/checkout
ownership before using the socket, as in the earlier K8S-92 qualification.

An empty, duplicate, unknown, unselected or repeated service fails closed. The
server exits after precisely the selected subset. No database or filesystem lane
needs to wait for unrelated identities. The current allowlist is the previously
approved 19 service identities; new consumers require explicit service-specific
identity approval and their own native fixture before joining it. Existing
application-native fixtures remain on their owners' branches, not copied into
this shared script. A 4096-byte transport proof is never a native application
acceptance, source retirement gate or permission to replace an incumbent point.

## Ownership and outstanding gates

The inventory's five confirmed prerequisites remain separately owned:

- Store native engine selection and physical capture owners are reported without
  duplicating shared PostgreSQL, MariaDB, Redis, Elasticsearch or object captures.
  Per-engine recovery execution and each consumer's coherence test are database
  lane work. Transport bytes alone cannot accept a shared service.
- Reusable encrypted NAS/R2 qualification is implemented here. Consumers can run
  independently with their own approved identity subset.
- Export tree comparison is implemented here. Actual writer fences, coherent
  generation receipts and original key/native boot tests remain application-local
  gates. The infrastructure capture and restore scripts on the application
  branches already provide CSI lineage and isolated transport rendering; this
  shared task does not edit their concurrent files.
- Numeric RPO, RTO, history depth, minimum copies and rollback-window approval is
  still absent from the inventory. Preserve incumbents and original keys. Do not
  infer approval from existing Kopia policies or successful synthetic drills.
- Per-app reports are executable and preserve original escrow references. Reading
  a reference does not prove escrow or recovery of the original secret/key.

This shared implementation can be consumed before unrelated application gates
pass. It is not evidence that the full migration, all native service recovery,
production deployment, escrow or retention approval is complete.
