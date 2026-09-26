# Tunarr dual-version Kopiur recovery

This is the Tunarr implementation for K8S-92. K8S-102 repaired runtime persistence and is closed. It is not the owner of this migration.

## Source contract

Read-only observation on 2026-09-26 verified the running image and durable paths after the K8S-102 rollback. The earlier pre-repair media-contract audit is not authoritative for Tunarr paths.

| State | Location under `/config/tunarr` | Image |
| --- | --- | --- |
| Active | `stable-1.3/db.db`, `stable-1.3/settings.json`, `stable-1.3/channel-lineups/`, `stable-1.3/images/` | `ghcr.io/chrisbenincasa/tunarr:1.3.15@sha256:ae8ec490459e773571b277e2f64248605c1ea64d49ee4d26aa01741409d60369` |
| Retained, do not downgrade | `db.db`, `settings.json`, `channel-lineups/`, `images/` | `ghcr.io/chrisbenincasa/tunarr:2026.9.0@sha256:af8790eb57dc3f3a4e9905859b789ec1fa519c5c537c2d0a996e25c22fe80d33` |

Both databases have WAL/SHM files. SQLite tables contain Plex media-source credentials, program metadata and direct NAS media paths. JSON settings and SQLite files are private recovery data. Never print them. The media files themselves live on the read-only `/media` NAS mount, not the Tunarr PVC. This policy backs up all PVC files, not the NAS media library.

The active source initially had 2 channels, 1,244 programs, 1,244 program_media_file rows and 17 channel_programs rows. The retained source had 2 channels, 175 programs, 175 program_media_file rows and 17 channel_programs rows. Ingestion is active, so use the selected recovery point's manifest counts, not these observations, as the restore oracle.

## Capture and failure handling

`kopiur-policy.yaml` contains the exact `scripts/tunarr-kopiur-capture.cjs` implementation. Its test enforces equality. Changes to the script require updating the embedded block.

- Whole-PVC CSI snapshot to NAS SMB, followed by NAS-to-R2 replication. Identity is `tunarr@media:/pvc/tunarr`. NAS path is `mnemosyne:Backups/kopiur/tunarr-smb`; R2 prefix is `storage-backup/kopiur/tunarr/r2/`.
- The hook does not stop Tunarr or checkpoint its live SQLite files. Node 22's real SQLite backup API captures both DBs read-only, including committed WAL state.
- Each version includes settings, channel lineups and images. Hashes bracket the database copy. The hook rejects missing/corrupt state, symlinks, changing companion files, mismatched channel UUIDs, missing lineup program IDs, per-channel distinct membership or full lineup duration mismatches against copied SQL, failed SQLite integrity/foreign-key checks and unexpected schema.
- Private generation directories and an atomic manifest preserve the last good copy. An attempt marker poisons stale success after failure. A directory mutex rejects concurrent captures. An abandoned lock after SIGKILL requires operator investigation, not automatic deletion. Failure never means a previous generation is current.
- The hook has a 90-second budget, checked across synchronous work as well as the async timer. WorkloadExec's outer timeout is two minutes, `continueOnFailure` is false. The parent owns shared mover deadline controls and controller fail-closed acceptance.
- The raw full-PVC snapshot includes search data, caches and logs. The app-consistent bundle treats Meilisearch as derived and preserves SQL media metadata plus authoritative JSON/images. Neither the live search index nor raw live SQLite files are the recovery authority.
- VolSync NAS/R2 resources remain intact. Do not remove either fallback before the parent proves both Kopiur backend restores and approves cutover.

The review corrections follow both pinned tags, `v1.3.15` at `b0c9176aef3949a50a1d37be8eecd911306448fd` and `v2026.9.0` at `530be0581f5e3fe6d35294136890def65d73671a`:

- `server/src/db/channel/LineupRepository.ts` commits distinct content membership and the sum of every item's `durationMs` to SQL before saving lineup JSON. Offline and redirect items contribute duration, not program membership.
- `server/src/db/derived_types/Lineup.ts` defines content, offline and redirect items with positive numeric durations. `server/src/db/converters/channelConverters.ts` reports content occurrences as API `programCount`, including repeats.
- `server/src/services/MeilisearchService.ts` auto-imports `ms-snapshots/data.ms.snapshot` when `data.ms` is absent. Both raw search paths must leave the promoted runtime tree.

## Remote verification

Run verification on the existing `ghar-set-zoo` ARC runners, not the operator workstation. The runner needs Node supporting `node:sqlite`, Python 3.11+, yq and its disposable Docker daemon. The host regression suite exercises real capture/restore code against a minimal SQL contract fixture, not the Tunarr runtime. Resolve only the trusted runner scratch root because restore validation rejects symlink ancestors. Never resolve an untrusted recovery path to bypass that guard.

Commands for the remote runner:

```sh
export TMPDIR="$(realpath "$RUNNER_TEMP")"
python3 -m unittest discover -s scripts/tests -p test_tunarr_kopiur_host.py -v
python3 -m unittest discover -s scripts/tests -p test_tunarr_kopiur.py -v
scripts/validate-app --offline media/tunarr
```

The native tests bootstrap each pinned image's real schema, seed disposable channels/programs without media credentials, use the image's Node SQLite backup API, and boot separately promoted copies. Each retains 2 channels, 17 programs, 17 distinct channel-program rows and 17 media-file rows. The revised fixtures include repeated content, offline items and redirects, expecting 19 content occurrences from the channel API. Native regressions include the SQL-commit-before-JSON gap, same-cardinality membership replacement and corrupt raw snapshot archival, alongside the existing WAL, deadline, corruption, symlink, freshness and mutex cases. Test container names are unique, all networking is disabled and cleanup targets only those containers.

Before review, both pinned 1.3.15 and 2026.9.0 synthetic schema fixtures completed isolated boots from separately promoted local copies. The newer image is not an untested compatibility blocker. These prior results do not verify the review corrections. The revised native suite awaits an exact-commit run on the existing ARC runners. Operator-workstation Docker capacity is not a release prerequisite. No shared images or caches were pruned. Actual production recovery from independently retrieved NAS and R2 snapshots remains pending.

Local `nas` and `r2` labels are separate promotion exercises from synthetic bytes. They do not prove NAS or R2 transport, replication, credentials or independent repository recovery.

## Separate restore and boot commands

The parent first retrieves a selected successful snapshot independently from NAS and from R2 into separate, private, offline directories. Do not point these scripts at the live PVC. Stop all writers to the retrieved directories. Preserve the full PVC-relative tree, including `.kopiur-consistent` and both versions. Record snapshot IDs, replication lineage, successful controller run, selected manifest generation and the controller's capture time window. Do not derive the acceptance window from the manifest itself. Use clocks with verified synchronization.

`--backend` records evidence supplied by the operator. It is not a backend connector or proof of isolation. Backend retrieval and network-isolation evidence remain parent prerequisites.

With absolute, non-symlink source paths, existing private destination parents and destinations that do not yet exist:

```sh
scripts/tunarr-kopiur-restore.py \
  --backend nas --source "$NAS_RETRIEVED" --destination "$NAS_PROMOTED" \
  --generation "$NAS_GENERATION" --capture-start "$NAS_CAPTURE_START" --capture-end "$NAS_CAPTURE_END"
scripts/tunarr-kopiur-restore.py \
  --backend r2 --source "$R2_RETRIEVED" --destination "$R2_PROMOTED" \
  --generation "$R2_GENERATION" --capture-start "$R2_CAPTURE_START" --capture-end "$R2_CAPTURE_END"
scripts/tunarr-kopiur-boot.py --source "$NAS_PROMOTED" --version stable-1.3
scripts/tunarr-kopiur-boot.py --source "$R2_PROMOTED" --version stable-1.3
# Boot each backend's retained state with its exact newer image:
scripts/tunarr-kopiur-boot.py --source "$NAS_PROMOTED" --version retained-2026.9
scripts/tunarr-kopiur-boot.py --source "$R2_PROMOTED" --version retained-2026.9
```

The restore script validates both versions, their complete file set, hashes, SQLite integrity/foreign keys, table counts, channel/lineup relationships, generation and completion window. It copies the full retrieved PVC to a new directory, archives raw databases/settings/lineups/images/search state under `.kopiur-raw`, including both `data.ms` and the auto-importable `ms-snapshots` directory, promotes the verified bundle, reads it back and emits a private completion receipt. No source changes. A partial destination has no success authority and must not be reused.

The boot script requires that receipt and verifies promoted bytes again. It makes a throwaway copy in Docker tmpfs, never bind-mounts the recovery directory or production media, and uses `--network none`, no published ports, no credentials injected from the host, dropped capabilities, non-root UID 568 and a read-only image. Meilisearch indexing memory/threads are limited in the test only. HDHR auto-discovery is disabled only in the throwaway settings copy because network-none has no SSDP interface. The app rebuilds search state from its database. It checks exact app version and SQLite channel/program/membership/media-file counts separately from per-channel API content-occurrence counts derived from verified lineups. It never requests a stream. The promoted source is unchanged and the disposable container is removed.

## Parent release gates

1. Resolve current K8S-92 ownership and the live indexing/IO maintenance blocker. Do not reopen or assume ownership of K8S-102. Avoid any viewer interruption.
2. Provision `kopiur-tunarr` in the approved secret store with independent `NAS_KOPIA_PASSWORD` and `R2_KOPIA_PASSWORD`. Existing `volsync-template` supplies R2 account fields. Do not disclose values.
3. Confirm source paths/digest have not changed; review this uncommitted diff. Shared namespace admission, mover deadlines and controller behavior are outside this change.
4. Deploy only in the parent's approved window. Verify ExternalSecrets, repositories, policy and schedule, then exercise the real hook and successful snapshot without interrupting playback. Inspect generation, attempt marker and controller outcome.
5. Independently retrieve NAS and R2 recovery points, then execute promotion and isolated boot against each. Verify table and API counts against each selected manifest and record snapshot lineage. Do not substitute the local tests for this gate.
6. Run the revised native regression suite on both exact digests using the existing ARC runners. Then validate the actual retained production state retrieved independently from NAS and R2 on the exact 2026.9.0 digest. Prior synthetic boots are not backend acceptance. Keep versions isolated; never boot 1.3.15 on the retained newer database.
7. Keep both VolSync recovery paths until explicit backend acceptance and parent-approved removal. Nothing here mutates live deployments or schedules by itself.
