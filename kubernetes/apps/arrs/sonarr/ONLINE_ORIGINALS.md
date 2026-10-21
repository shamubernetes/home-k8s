# Non-disruptive native original acceptance

Existing actual native NAS/R2 PostgreSQL original recovery proof is retained:
GitHub recovery run 38018173473, head 9330f11027f63a977c978bc3fc942e9a51114006,
conclusion success. It is not replayed or replaced with a fixture.

The remaining producer defect was the global file-tree quiet requirement.
The app's existing helper mounts use subPath and cannot be hot-updated safely.
An independent immutable-name ConfigMap now supplies an inline workloadExec
command to the existing dedicated non-elevated kopiur_sonarr PG17 client.
Production Helm values, helper subPath mounts, running Pod/container and restart
counts are unchanged. The actual online capture completed in 17 seconds.

The new source uses a single read-only PostgreSQL transaction, full native archive
TOC/checksums and only actual configuration-input stability. No application stop,
freeze, restart or writer hold, and no global ordinary file-tree quiet period.
The subsequent online CSI point preserves complete ordinary configuration/state.
Actual fresh NAS 078628d9342dcbccf2c3793dc07436b6 and its independently verified
R2 copy cbc9bf6e72ed227a14013ca2e1cabfb9 both succeeded before legacy retirement.
The accepted native recovery/data proof above remains valid for the unchanged
native PostgreSQL archive protocol and original application configuration.

Only sonarr-primary is active, daily 01:45 New York with ten-minute jitter.
The duplicate dormant sonarr-daily remains suspended. Offsite is daily 02:45 New
York, latest current point only, no pruning or accepted-history replay. The exact
original bound PVC/PV is retained and prune-protected. Existing NAS/R2 Restic
history ExternalSecrets and unchanged access survive source/bootstrap retirement.
