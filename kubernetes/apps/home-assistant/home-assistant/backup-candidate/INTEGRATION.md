# Executable integration handoff

This is an unregistered source-only candidate. Do not register the root while its
prerequisites remain false. Existing PodTemplates and VolSync resources are
unchanged. Backend ExternalSecrets now reference verified provisioned fields only.
They are source manifests in this unregistered root, not proven live ESO sync.

`integration-prerequisites.json` is the exact nonsecret Secret/key and minimal
shared deadline delta contract. Consume only verified provider/item/field metadata
from `acceleration-provision-handoff.json`. Do not inspect secret values or race
backend provisioning. Add ExternalSecrets only after exact fields and provider
state are verified, then require ESO Ready and key-presence checks. The dedicated
backend identity worker does not provision the coordinator's native DB roles.

The shared delta adds namespace-and-manager-scoped 3600-second caps for the exact
config/maintenance/verify/replication labels and delete operation in that JSON.
The dedicated test constructs that proposed policy in scratch and runs real
Kyverno positive and negative fixtures. It does not edit or deploy shared policy.
Hook Jobs already have a 600-second deadline. Verify controller hook permissions
and AdGuard's separate privileged-mover namespace opt-in before activation.
No broad namespace allowlist, cross-app identity or runtime PodTemplate change is
needed. The coordinator owns the shared policy/RBAC/DB-role merge and live writes.

Before registration render this root and server-dry-run all resources and embedded
Jobs. After prerequisites pass, register only this app, capture one fresh original,
replicate that exact point and qualify independent NAS and R2 native restores.
Keep existing recovery history and VolSync until the per-app acceptance gates pass.
Daily schedules, Retain, maxErrors=0 and strict read errors remain unchanged.

## Native archive execution

The real 2026.8.0 `_mkdir_and_generate_backup_contents` implementation with its
pinned securetar 2026.4.1 generated the committed synthetic fixture. The test
validates its nested config/auth payload and native database exclusion. This is
native archive-format evidence, not a running Home Assistant recovery.

The downloader now has a 480-second whole-operation deadline and a 1 GiB byte
budget, rejects unsafe/duplicate/link/special members and requires a literal
external-database exclusion. Raw config and Git PVCs remain separate sources.
Do not silently ignore a required linked custom component; preserve its original
bytes and establish an explicit native restore contract before activation.

After dedicated token and read-only PG role provisioning, the coordinator must
verify the effective running recorder is PostgreSQL, not merely the declaration
in the Job. Render the root, extract the beforeSnapshot Job and server-dry-run it
before a single owned qualification capture. The native token is admin-only and
must be the dedicated backup identity, never an unrelated service token.

Restore `current/homeassistant.tar` and `current/database.dump` from the same
bound original generation. Run `capture.py`'s `validate_archive` before extracting
with the installed native recovery tooling. Restore the dump with PG17, then boot
the exact HA image against that isolated recorder, recovered auth, config and Git.
Block automation/device/network egress. Test original auth and recorder history.
Record native/dump/CSI timestamps, process birth and HTTP continuity. Native local
archive retention remains an activation gate: use `backup/info`, retain the
owned generated backup_id from `native.json`, and only after independent bundle
acceptance delete that exact owned native backup through `backup/delete` and read
back its absence. Never bulk-delete existing native backups.
