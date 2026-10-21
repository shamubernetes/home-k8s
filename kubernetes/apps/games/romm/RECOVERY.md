# Original recovery and legacy retirement

K8S-92 independently restored NAS `6b7a100debd772d97a847d3cef9fa89a` and R2
`0f6f87ad1df558866e6f6ff9a5e9ceef`. Native PostgreSQL 17 Job
`k92-romm-pg-native-resume45716`, UID `863d862c-2eef-418b-925d-73459848dd44`,
completed on 2026-10-21 at 08:06:27 UTC. All original user-schema tables were
restored with pg_restore --exit-on-error and full native row fingerprints matched.
Native table fingerprint: f1234ad24c020d5138b9d404f5972bb5c106a9f2ddc531310d6ba36d23c4a87b.
All 283 original files, 5,082,635 bytes, matched complete-tree fingerprint
861d171157d86f7e1225bab1e49daf71b8d2e3479166a4257860c07499999b99 and remained unchanged.

The format-3 generation under .kopiur-postgres/current contains romm.dump,
romm.toc, application-config, ROMM_AUTH_SECRET_KEY, metadata and SHA256SUMS.
Every artifact checksum was verified. Restore the full ordinary original tree,
then replay the native archive into an isolated PostgreSQL 17 database; do not
replace it with a fixture or later live dump. Retain the original projected
application configuration and auth key. Existing GitOps and 1Password sources
provide other unchanged runtime settings and service credentials.

Actual original-image 5.4.0 Job `k92-romm-native-key-resume45716`, UID
`c2388bb5-8554-4b65-b98a-73387974f37c`, completed at 08:19:13 UTC. Its installed
joserfc 1.7.5 session JWT engine signed with the original NAS key and verified
with the original R2 key, using RomM's HS256 contract. A wrong key was rejected.
The original key also matched the effective running production environment.
No original secret values or generated tokens were printed or persisted in
manifests. Native database/config/key acceptance is complete, not a browser login.

Daily capture is 21:00 New York and offsite copy 22:00 New York, with five-minute
jitter. The established single native read-only PG17 transaction, stable config
and key projection, followed by a complete online CSI source point, is retained.
No application stop, freeze or writer hold is used. The exact existing PVC is
retained and prune-protected. Existing Restic NAS/R2 history access stays in the
same ExternalSecrets after VolSync source/bootstrap retirement.
