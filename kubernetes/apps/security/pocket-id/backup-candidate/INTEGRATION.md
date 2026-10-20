# Executable integration handoff

This is an unregistered source-only candidate. Do not register the root while its
prerequisites remain false. Existing PodTemplates and VolSync resources are
unchanged. No ExternalSecret has been invented or registered.

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

## Native database and object execution

The hook now restores its own `database.dump` into an isolated temporary PG17
server, with no TCP listener or source DB credentials, and executes `references.sql`
against that restore. The query follows native v2.18.0 light/dark client-logo keys,
including dot-containing ordinary client IDs and hashed CIMD URLs beneath the
configured S3 `uploads` root. Native schema drift fails the query.

`validate.py` requires every restored DB logo reference to exist in the bucket
inventory, every observed object to have exactly matching local bytes and size,
and both inventories to agree independent of order. It rejects unsupported
multipart/encrypted ETags rather than pretending they are MD5, missing references,
extra files, unsafe keys, links and non-private/missing original encryption keys.
Profile-picture and global-image objects are all retained as bucket bytes; their
optional/fallback behavior must still be exercised by the native app, not inferred
from nonexistent database columns. The final publisher runs only after these
checks and keeps the previous complete generation on failure.

The real fixture applies all 68 upstream PG migrations at commit
`40cb14f89309286dcafaa62dac82a88a4b5ac583`, dumps using a distinct SELECT-only role,
restores with native PG tools and compares native light/dark/CIMD references.
It also proves that role cannot write and that dropping a required column fails.

Before activation verify the existing `pocket-id-bucket` identity has List/Get
permissions. Provision the distinct `pocket-id-kopiur-postgres` role separately.
Project only the original `pocket-id-secret/ENCRYPTION_KEY`. Never generate a new
key or import the whole runtime Secret into an exporter.

Run the rendered native Job once through the approved coordinator. Independently
restore the same NAS and exact-lineage R2 bundle, put all `rgw/` keys into isolated
RGW, restore PG17, and boot exact v2.18.0 with that ORIGINAL key. Compare original
users, clients, passkeys, JWKS and a real original authentication path. A different
key must fail the decryption/auth test. Reference/byte validation does not itself
prove that test or global cross-store atomicity.
