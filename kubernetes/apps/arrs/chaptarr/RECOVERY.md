# Native original recovery

K8S-92 independent NAS `b15de742ebb7ff40874613aef2d3ea2d` and R2
`3e7f2ed9e5d1b39be9bb33163e45b68b` controller restores completed into distinct
non-production PVCs. Session-owned native Job
`k92-native-generation-chaptarr-cli8623`, UID
`ee88e466-5597-45de-adce-49a649f0c21d`, completed successfully using PostgreSQL 17
from the same pinned capture/restore image. All three actual original databases
restored with pg_restore --exit-on-error, then every public table's complete
native row fingerprint matched across NAS and R2. The temporary PostgreSQL
server listened only on its local Unix socket. No production DB or credentials
were used by recovery.

Original native content fingerprints:

- chaptarr_main: b1b6fccbe8c815b9ec2f4accda5249e0e07920bb4fb6c8c11c0ee1f82a376597
- chaptarr_log: 3fb718ec3e789a97bff59cbb877386cb6735cc93c67edae58101a22b9d77efbe
- chaptarr_cache: 911d4a56af54729591b6b1ac4d4fd0e35c7024d2abe36f26662335379994229d

Both complete original trees matched
`cec551f612d98fe0506f1422ac5b5c4574a8f80366107cdae13c5b425ca975a7` and remained
unchanged. All original config/key bytes were retained. Actual Chaptarr config.xml
contains session/OIDC settings and has no ApiKey element, so the consumer does
not invent one. Every native PostgreSQL table is restored, including persistent
application settings.

The immutable originals contain one complete native generation,
`9d385cb0-1fcb-496a-8795-aa224e66d86a`, with all three dumps, SHA256SUMS and metadata,
but no mutable current pointer. The consumer accepts the existing pointer when
present, otherwise requires exactly one complete generation or an explicit
ORIGINAL_NATIVE_GENERATION, and verifies every archive hash. No capture, original
transfer or accepted native proof needs replaying.

Daily non-disruptive capture is 20:16 New York, with the existing daily offsite
copy preserved. The original PVC is prune-protected before legacy retirement.
The production Pod, configuration, auth source and native writers remain intact.
Native receipts and exact helper checksums are also retained in Kaneo K8S-92.
