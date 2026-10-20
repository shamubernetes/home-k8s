# SABnzbd recovery

## Actual effective-state replacement

The accepted production original is NAS `8fc3f183d613d3c850b86aa5fcaafdbe`, copied to R2 `d424cbd8fe4c805bb45675881198c549` with exact copiedFrom lineage. Both points are retained. This original contains the effective native history, 47910 records across two SQLite tables, not the stale 207-row database found under the older config path. The no-stop online capture resolves the authenticated native effective admin directory and binds saved configuration and both running API/NZB keys in manifest v2. Existing configuration and 1Password references already preserve those keys; no second credential archive is introduced.

Strict native controller Restores `sabnzbd-k92-effective-v3-nas` and `sabnzbd-k92-effective-v3-r2` completed into PVC UIDs `8e2fc5e4-7be1-4a3b-a746-03c0bc75888c` and `b5626545-735a-4051-9a22-5d5f638a14a4`. These are separate from production. No ignored restore errors, skipped owners, skipped permissions or skipped times were used. Observed mixed-owner chown failure was repaired with the supported bounded UID0 mover and only CHOWN/FOWNER/DAC_OVERRIDE, not by ignoring errors.

Exact-image native Job `sabnzbd-k92-effective-native-v6`, UID `5f8fa502-5ea9-46f5-b168-85f7ef9d4a5f`, completed at `2026-10-20T23:21:00Z`. Each independent original booted SABnzbd 5.1.3 twice from disposable copies. All four boots passed authenticated native API, actual captured-runtime key binding, effective recovered API/NZB key equality and exact original SQLite-record equality at native schema8. Independent complete original config/file bytes matched across 18 members. Database file SHA256 `3e88f533f5115bd06c813ed3142dff4c5caeb0a100997cc74d33b3a0582098d2`; logical record fingerprint `69091dff14e379bae96572e3e88f73f0e8cc64612de07328403278e5f19a271a`.

Only isolated disposable downloaders were paused and stopped during this test. Production remained running with the same Pod and zero restarts; its read-only authenticated queue probe reported four active slots and paused=false. Existing external NAS Downloads queue/payload mappings remain untouched and are outside the replaced config-PVC backup scope. This migration neither promises a new whole-Downloads backup nor discards those existing bytes.

## Daily protection and retained history

The supported existing daily capture remains enabled at 02:10 America/New_York and offsite copy at 08:10, using the corrected effective-state hook. Their cadence is daily, never hourly. Current accepted effective capture, exact offsite copy and actual independent native recovery prove replacement; another calendar-day wait or duplicate restore mechanism is not a gate. Source and offsite retention protect existing recovery history.

Before retirement, production PVC UID `624725f0-e8c9-4e35-81db-749eee46468e` was verified Bound and prune-disabled. Removing its old bootstrap declaration must not delete that bound PVC/PV or change the app's existingClaim. Its immutable historical dataSourceRef is retained provenance, not a runtime dependency or future restore bootstrap. The two legacy NAS/R2 ExternalSecrets and generated Secret identities/provider references are preserved to keep all old Restic history accessible. No backend history is deleted.

## Independent recovery

Use either exact retained Snapshot with a fresh Kopiur Restore target, require terminal Completed/Ready and exact manifest identity, then use the checked-in isolated native verifier against read-only restored originals and disposable writable copies. Recover the standalone manifest-v2 SQLite history to the native admin directory and preserve the original effective keys/config mapping. Never test by restoring over production or by pausing the live downloader. Deliberate adoption into a new application claim is a separate GitOps action.
