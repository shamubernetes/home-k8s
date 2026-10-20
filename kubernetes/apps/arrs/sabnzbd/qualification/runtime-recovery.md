# SABnzbd effective-state audit and delivery

Source-only audit on 2026-10-20. No production capture helper, writer hold,
credential change, rollout or backup-backend operation was executed.

## Finding that changes acceptance

The successful retained-original proof is valid for the bytes it recovered, but
its 207-row, schema-5 history database is not the running application's database.
Read-only native `get_config` and process-open-file inspection identify a distinct
external admin database with 47,909 history rows, schema 8 and 53,960,704 bytes.
The old hook's fixed config-tree database is 258,048 bytes. Their inodes differ.
One native process had ten descriptors referencing the effective database.
The production Pod UID was `b2c38a51-0a06-422b-af48-25b5f7df2c2e`, with zero restarts.
The first inspection timed out; the bounded successful observations are separate
from that invalid attempt.

The minimum production repair is in the existing inline policy hook. It resolves
`admin_dir` from the native loopback API using the existing application key,
performs the same bounded WAL-aware online SQLite backup, checks effective runtime
and config stability across the copy, and records version-2 source/config hashes.
The logical recovery target remains `admin/history1.db`. No mount, sidecar,
HelmRelease, schedule, retention, verification or incumbent-history change is
needed for this repair. Missing API/key/source or unsafe paths fail closed. There
is no fallback to the stale database and no proxy forwarding of the API key.

The native verifier accepts retained v1 originals unchanged and checks v2 config
binding and provenance. It binds recovered config keys to the captured effective-key
hashes before starting a consumer, then checks both keys through the real native
config API. The manifest adds hashes only, not another credential archive. It
remaps config-backup output as well as the other output paths inside the
disposable consumer.

## Runtime/key gate already satisfied for the retained config

The live ini SHA256 is
`b6079241deb964b22fa48846df858fe5b695ccc83d08bd9ca8af8e79870cb872`,
identical to the actual retained v4 original-pair receipt. Both runtime keys equal
both saved ini keys and both effective native API keys. No replacement key or
second plaintext credential archive is required. Keep the original protected
config, repository passwords and existing ExternalSecret access. Do not rotate
or reuse another service's credentials for recovery.

The six relevant runtime environment fields map to the two saved keys, persisted
port and whitelist entries, source-declared timezone, and one immutable-image
default. The image-default name hash is
`73ed34093794774db426f6bf883e090bf53284644d07d174fcfa3f9d65dd4b0d`,
and its value hash from the registry image config is
`687e26302633993ab5e4b65dd0b604df32d6a923967f31c216231b4e0befbba3`.
The broad 'all environment fields persisted as misc keys' predicate is false
because that image default is not a misc key. Do not treat that predicate as a
missing credential. Recheck its equality to the running environment before final
runtime acceptance. Native key replay of the corrected generation is still a
separate execution gate.

## Effective external paths, without private names

All seven configured external roots resolve inside the already-declared SAB
Downloads NFS submount. No configured category directory overrides were found.
These are immediate-child counts, not recursive byte inventories.

| Native setting | Immediate contents | Mode, UID:GID |
| --- | --- | --- |
| download_dir | 386 directories | 02777, 99:100 |
| complete_dir | 157 directories | 02777, 99:100 |
| admin_dir | 6 files and 1 directory | 02775, 99:100 |
| nzb_backup_dir | 81,257 files | 02777, 99:100 |
| dirscan_dir | 0 entries | 02775, 99:100 |
| script_dir | 0 entries | 02775, 99:100 |
| backup_dir | 1 file | 02775, 99:100 |

`log_dir` resolves to the existing config logs emptyDir, with three files. The
policy protects the PVC, not that overlay. The password-file setting is empty.
HTTPS is disabled and the configured default certificate/key paths do not exist.
This is observation, not permission to exclude logs or any other bytes.

The config scan found 13 files, 9 directories and no symlinks, including the logs
overlay. Modes include 0600/0644 files, a 0700 backup directory and mixed owners
0/568 with setgid directories. Do not normalize these to UID568/mode0755.

The current PVC policy does not capture the external Downloads tree. Capturing
the correct history database does not protect queued admin state, unfinished
payloads, completed payloads, archived NZBs or configuration backups. Empty watch
and script directories still need their mappings and metadata retained. Existing
NAS protection of those external bytes was not established by this audit. Do not
call them recreatable, caches, out of scope or protected merely because NFS is
persistent. A real recursive byte/metadata inventory and independently tested
NAS/offsite protection remain required. Bind native queue state to its recovered
external payloads without holding writers. Do not manufacture global atomicity.

## Metadata replay needs execution, not a new metadata implementation

Existing native Restore options already expose owner/permission/time replay. The
retained original Restores deliberately set all three skip flags true. Their tree
verifier compares names and bytes, not metadata. They therefore prove no metadata
replay, even though both trees are equal.

Keep those completed resources and receipts intact. Parent should create two new
Restore names and new disposable PVCs for an exact selected NAS/R2 pair. Set
`skipOwners`, `skipPermissions` and `skipTimes` false, with both ignore-error flags
false. Omit fsGroup to avoid recursive metadata rewrites. Use the installed native
mover's supported root/capability contract only on these disposable destinations
when required for mixed ownership. Do not run the application as root or alter
production permissions. Compare every restored member's owner, mode and stored
modification time at the precision available in each retained native manifest,
including root directories. NAS/R2 equality alone is not equality to original
manifest metadata. Reject any restoration that skipped or could not replay an
entry. Metadata replay remains unpassed until those exact receipts exist.

## Minimum parent delivery and proof

1. Review/cherry-pick the local candidate. Run canonical app validation and the
   Talos scope classifier. Publish only under parent's existing authority and a
   verified maintenance scope. The application PodTemplate must remain identical.
2. Watch normal Flux convergence to the exact merge SHA. Read back the delivered
   SnapshotPolicy hook and its generation/hash. Bracket native PID/start identity,
   Pod UID/restarts and authenticated read-only API checks. No helper mount or app
   rollout is needed for an inline policy-hook change.
3. Capture a new corrected generation through the real controller, then copy its
   complete retained history to R2. Resolve the exact copiedFrom lineage. Require
   terminal NAS/R2 success and the v2 manifest's config/source/runtime hashes.
   Preserve the old NAS/R2 pair and all VolSync histories and access material.
4. Restore that corrected pair independently into fresh PVCs distinct from
   production. Use new names rather than rewriting the completed v4 Job or old
   Restore specifications. Validate full original trees, config binding and
   standalone SQLite integrity. The effective history is schema 8, so the old
   empty-RSS migration exception is not a substitute for equality of all its
   captured tables and records. Record actual corrected source counts/hashes,
   not a hard-coded 47,909 expectation while production remains writable.
5. Publish a freshly named deny-all/tokenless exact-image native proof Job using
   the updated verifier and only disposable writable consumers. Require version,
   paused queue, both native recovered-key comparisons and unchanged original
   history through initial/restarted consumers for both backends. Bind the first
   receipt to Job UID, image, pair IDs and destination PVC UIDs.
6. Execute the metadata recipe above and separately protect/recover the external
   Downloads bytes and queued native state. The current native proof deliberately
   remaps Downloads to empty disposable directories, so it cannot prove queue or
   payload recovery. Size its disposable storage from the real inventory first.
7. Observe a naturally eligible daily corrected capture, exact-lineage offsite
   copy and integrity verification with continuity observations. A later explicit
   daily UTC slot today is possible under parent authority; no hourly trigger or
   application pause is introduced by this candidate. Retire nothing until every
   applicable gate is passed.

Local SQLite/HTTP fixtures are actual executions of the embedded hook, including
an active committing writer. They are not a controller capture, metadata restore,
external-byte restore or an installed SAB native boot. No local Docker was used.
