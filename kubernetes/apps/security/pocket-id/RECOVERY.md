# Original recovery and legacy retirement

K8S-92 independent originals NAS `e4fefe1aec1218aff325b6aa61b7d2b1` and R2
`6f00e621194137be8c2a578bb9a84f9b` passed native PostgreSQL 17 replay in Job
`k92-pocket-id-pg-native-resume45716`, UID `46238e85-680d-459f-9b35-3ad3e8dd56fe`,
on 2026-10-21 at 08:06:24 UTC. Every original user-schema table's complete native
row fingerprint matched: f102d4e6db2b8f8ecec9b73cbe65ceaa4661917989188224534944c891495590.
All 36 files, 2,406,584 bytes, matched tree
d77c83b71916b909283c51e9b70ee4859ca39652644a5f833d8dae00a88c7d5c and remained unchanged.

The native current directory holds database.dump, ENCRYPTION_KEY, RGW objects,
before/after object inventories and original database references. All 30 captured
objects passed original size/ETag and full SHA-256 validation; all 20 referenced
object keys resolve. The stability contract applies to actual original DB
references, not an unrelated whole-bucket quiet period. The original encryption
key remains private and paired with the actual native database.

Original-image 2.18.0 Job `k92-pocket-native-key-resume45716`, UID
`fbc35bf7-84d6-4d42-98f9-52ce03370a06`, completed at 08:29:22 UTC. On isolated,
network-denied database copies, the real supported Pocket ID encryption-key-rotate
command loaded the original encryption key through ENCRYPTION_KEY_FILE and
successfully decrypted both retained issuer/session JWK rows for each backend,
including native SCIM processing. Only those disposable copies were re-encrypted
with a public test key. Production and the read-only originals were never changed.
That test operation is not part of ordinary recovery and must not be applied to
production. Ordinary recovery retains the original encryption key.

Current production Pocket ID mounts no PVC: effective durable state is PostgreSQL,
RGW and original runtime keys/configuration. Daily native capture protects that
state at 19:20 New York, with offsite copy 20:20 New York. The dormant original
legacy PVC remains bound and prune-protected without modification, with retained
Restic histories and unchanged access. No production PodTemplate is changed.
