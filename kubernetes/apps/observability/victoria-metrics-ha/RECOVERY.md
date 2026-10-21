# Native originals and daily online source

NAS original 5c6eb6040bdbb0aec2563c0a6aa98c65 and independent R2 original
4a1aa4017e5a64faaace5e80763ac15b passed complete original validation: all
8,161 files and 4,083,539,892 bytes match tree
32ad890fc799c4d15ee85a2c024c3dc843371c3bcc5eae2a39aca3025f1541ef.
Actual validated preparation Job UID c2c8aa96-de77-4673-8050-f88e3797c786
kept both originals unchanged. Its valid full-tree evidence was reused after
checking original PVC UIDs, no live writable mounts, and read-only consumers.

Native v1.154.0 Job k92-vm-native-resume45716-v3, UID
9eb85963-6dbc-4339-91c2-edb09551a027, completed 2026-10-21 at 09:10:11 UTC.
Both exact original engines recovered all 450 metric names, equal actual query
data and all 43 samples of the selected original series. Native sample hash:
8fecb127af241af08eb53da3002b262854281eed9fa689418e74a02903c74bc7.
Catalog hash: 95f33b3c7057f2f0953e91a3bff29fe89ace0309bf3382872752a12f155224c6.
The former raw JSON comparison differed only in stats.executionTimeMsec.
Every semantic query result and native sample equality check remains in place;
request execution timing is not application data.

VM native snapshot directories contain links into sibling immutable snapshot
partitions. Materialize only links contained in the restored original root before
starting a private engine. Copying the links alone produces a dangling view.
Never follow a link outside the restored original or boot a production mount.

Daily protection now uses the actual server-volume-victoria-metrics-ha-0 PVC,
not the frozen original clone. A short-lived co-located Job calls the supported
online native snapshot API, materializes its immutable partitions by same-volume
hardlinks under .kopiur-vm/current, and deletes only its own temporary API handle.
It never changes permissions/ownership of linked production inodes. No writer
stop, hold, restart, sidecar addition or production PodTemplate change is used.
The subsequent ordinary online CSI point includes that portable native directory
plus the whole source volume. Mover fsGroup is deliberately omitted so cloning
and read-only capture do not rewrite source file group metadata.

COMPLETE.json records the format, exact native snapshot handle and inventory.
For isolated recovery of this new producer format, validate that manifest, copy
.kopiur-vm/current to disposable writable storage and point the exact native VM
engine's -storageDataPath at that copy. Legacy accepted original points use the
contained-link procedure above. Daily primary is 01:47 New York, offsite 02:47.
Private materialization staging rotates separately from retained repository and
legacy histories. A fresh production-source request follows actual observed
policy/helper delivery; neither old frozen clone nor an unobserved candidate is
called current daily protection. VolSync retirement remains a later stage.
