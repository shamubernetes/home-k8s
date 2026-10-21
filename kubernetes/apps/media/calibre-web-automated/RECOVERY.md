# Native original recovery

K8S-92 independently restored NAS `b2f45c41a1a48946d5ff359382e592f4` and R2
`1f5f821db6485ee61ded26f51f1eed97` to separate non-production PVCs. Native Job
`k92-native-generation-cwa-calibre-cli8623`, UID `feb04f2d-ddb7-47db-b61a-01a5cee44642`,
completed on 2026-10-21 at 06:55:20 UTC using the exact production CWA image.
All five effective databases passed integrity and complete logical-data equality
on both independent originals. This includes every virtual and shadow table in
Calibre notes.db, using shipped Calibre 8.8.0's native APSW connection and its
sqlite_extension tokenizer. Plain sqlite3 cannot read the calibre FTS tokenizer
and is not a substitute. No table or read error was ignored.

The complete original trees matched fingerprint
`9fa637b1aa39c069de0ef658c941749596965edafc589c72545c36cb257eee5c` and remained unchanged. Application
settings, users, keys, Calibre library bytes inside the existing config PVC,
and retained incomplete historical capture bytes remained protected.
The external ingest/NAS tree is not newly added to this migration's scope.

These frozen originals contain complete self-describing native generation
`4a16244f5f0741409cfe2f6955920e2b/manifest.json` and a separate incomplete attempt,
but no mutable current.json pointer. The consumer uses the pointer when present;
otherwise it requires exactly one complete native manifest or an explicit
ORIGINAL_NATIVE_GENERATION. It validates all five expected paths and original
sizes/hashes before replay, rejects ambiguity and partial generations, and never
falls back to later live files or raw databases.

Daily capture is enabled only after this native acceptance, at 21:16 New York,
with the existing daily offsite copy preserved. The application is not stopped,
held or restarted. The original PVC is prune-protected before legacy retirement.
The saved v3 drill Job remains suspended to avoid replaying accepted recovery.
Supplementary receipt cli8623-cwa-calibre-native-result.log and exact source
checksum a41e2395063fa704c2e03a48dab40ecb688f697b1c44eae153bfc22d63434a83 are retained with Kaneo K8S-92.
