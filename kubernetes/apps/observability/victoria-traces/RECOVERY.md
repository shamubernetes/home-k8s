# Native originals, live protection and retained history

K8S-92 native Job `k92-vt-native-cli8623`, UID `ea11fc8f-322a-41f6-ae93-f04a2117aeb5`, completed
2026-10-21 at 07:40:02 UTC using both independent immutable original restores.
All 7,367 files and 5,904,747 bytes matched full-tree fingerprint
`dd9dcc29f20a367df4592f9a54ef37da137e505ca22247cfdf4d07af650e7d0a`.
Both exact original v0.12.0 engines recovered the same real service catalog,
complete trace and spans. Native trace fingerprint:
`84c9592d20d09c535004b298fcf710633df8f1a61bcd2c1b24de13b45a4e6b18`.
Only isolated writable copies were booted; restored originals stayed read-only.

That accepted native proof is reused, not replayed. Current protection no longer
copies the retained clone. Actual policy generation 2 observed 2 points to original
production PVC server-volume-victoria-traces-0, UID
`2cd4ccaa-dd57-40a3-a553-ba9a856d9dd0`, using fresh online CSI source points.
Fresh NAS `eacc10c3ef1a793facd4297d95ac9d59` and its exact successful R2 copy
`35f751b5941664aea0c5d9da11fd6082` were observed before source retirement.
Daily primary is 01:46 New York and offsite 02:46 New York, without hourly capture.

The original PVC/PV, running Pod/container and restart counts stayed unchanged
through fresh capture. The PVC is explicitly retained and prune-protected.
The two existing Restic history ExternalSecrets remain byte-for-byte equivalent;
only their VolSync source declarations are removed. No bootstrap destination is
present for this workload. Snapshot/restore source points, retained original CSI
objects and recovery histories are not deleted.
