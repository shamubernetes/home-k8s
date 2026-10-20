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

## Private-system and vector execution

The helper now requires a nonempty, readable private `_system` tree. It hashes
all private files before public capture, copies them into `private_system`, and
requires source/copy hashes to remain equal through export and escrow. Links,
special files, missing private state and OAuth churn fail closed. Refreshed OAuth
bytes and their hash accompany the effective config. Raw system/auth/model PVCs
remain additional independent CSI sources, not substitutes for a coherent bundle.

Native dense validation now rejects a package without dense vectors. Merely
calling `read_dense_vectors` can return an empty dict for a package with no dense
snapshot; that cannot satisfy the installed restore `vector_mode=require` path.
Native API/validator contracts were read from the exact deployed image digest.
Private-tree/OAuth regressions execute on real files, not a fabricated API success.
No full native image boot is claimed. Local Docker currently cannot create even
an existing-image container because its internal disk is full; no shared Docker
cleanup or production resource was changed to hide that blocker.

The coordinator must qualify `/app/.openviking/data/_system` against the actual
installed backend, execute the inline helper once through the existing
`workloadExec` hook, and retain all three sources and capture receipt together.
Do not roll the application to mount this helper. On isolated original NAS and R2
restores, verify private-system/OAuth hashes before boot, restore public OVPack
via temp-upload plus `/api/v1/pack/restore` with `vector_mode=require`, recover
private identity/catalog state and the model PVC, then cold-boot the exact image
with provider/production egress blocked. Exercise every actual account, original
OAuth/config provenance, model availability and vector retrieval. The helper's
raw model source and a public OVPack alone do not establish those gates.
