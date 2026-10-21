# Native original acceptance and retained history

K8S-92 independently restored original NAS cf6d18227d380d7b4050a3657a8696c0 and
its exact R2 copy fdde2dbabe409ca8a1c0b7cb959d9226. Both complete ordinary roots
were restored with strict Kopia content/metadata checks. R2's first 3600-second
attempt timed out during measured storage contention; bounded same-point resume
reused only complete atomically written files in the same isolated 150 GiB target.
No accepted point was recaptured, no history was pruned, and no production restore
was attempted.

Native original Job k92-plex-native-resume45716, UID
b6235b62-5bf8-4087-8c28-24c20f3040ff, completed 2026-10-21 at 10:40:05 UTC.
It used the actual original application image
`ghcr.io/home-operations/plex:1.43.4.10903@sha256:fdc6fe82bc7d8acd08fc0aa581fcb76722803ba5905e2537135a697347dd6149`.

The native Plex SQLite binary, not stock sqlite3, verified both original databases
and complete native all-table dumps. Every dump matched across independent NAS
and R2 originals; integrity_check returned ok for both databases on both backends.
The source format-2 manifest and every native artifact checksum were verified:

    library database: bf8e73ea65df447cb1337d4490d52e7973126124c3791cd8d08d954539b1e211
    blobs database:   e50eda8d62c073cac794ef9fe4ef1c86c47a6a7e3181e3b0e538c06fe32b9a48

Original Preferences.xml and the provisioned machine identity were preserved.
No token values, original configuration values or database rows were printed.
Only native database copies were opened with -readonly; originals were mounted
read-only, and the private consumer denied all network and service-account access.
Full ordinary restored roots remain available without an unnecessary additional
43 GB full-tree scan or repeat transfer.

The established online producer uses the native SQLite .backup protocol and a
subsequent complete ordinary CSI point. Regenerable Cache/Media/Logs and mutable
raw SQLite/WAL files remain excluded as documented in the existing policy; native
self-contained database artifacts and required original persistent configuration
are retained. No application stop, freeze, restart or writer hold is used.

Daily source is 18:00 New York and offsite 19:00 New York, with five-minute jitter,
no hourly schedule. Offsite selects the latest current point and never prunes
retained histories. Existing bound PVC/PV, production Helm values and PodTemplate
remain unchanged. The exact original PVC is explicitly prune-protected; existing
Restic NAS/R2 ExternalSecrets preserve unchanged history access after source and
bootstrap retirement.
