# Scoped backup database identities

K8S-92 separates backup-role provisioning from application capture rollout. This prerequisite creates five read-only roles across six databases. It does not deploy capture policies, change application credentials, retire incumbent backups, or certify full application recovery.

The Flux Kustomization `kopiur-backup-identities` in `arrs` waits for the five application Kustomizations and the External Secrets store. Each bounded Job uses its existing application Secret for database administration and ownership, and a separate ExternalSecret for only the approved backup username/password from the service's `kopiur-*` 1Password item. No admin credential is mounted into a backup mover.

| Service | Role | Allowed databases |
| --- | --- | --- |
| Bazarr | kopiur_bazarr | bazarr |
| Radarr | kopiur_radarr | radarr_main |
| Radarr 3D | kopiur_radarr_3d | radarr_3d_main |
| Sonarr | kopiur_sonarr | sonarr_main |
| Whisparr | kopiur_whisparr | whisparrv3_main, whisparrv3_logs |

The checked SQL rejects unexpected database ownership, schemas, RLS, large objects, elevated roles, memberships, security-definer execution, column/table writes, sequence mutation, and unsafe future ACLs. It grants CONNECT, public-schema USAGE, and SELECT on present and future application-owned tables and sequences. It does not grant `pg_read_all_data` or role membership. PostgreSQL PUBLIC defaults are not tightened globally by this change.

Provisioning uses TLS and suppresses SQL diagnostics, including server-side error statements before password-bearing SQL runs. A Job succeeds only after authenticating with the dedicated backup identity. Read back the exact Flux revision, all five completed Jobs, all five synced ExternalSecrets, role attributes, memberships, scoped SELECT privileges, and denied unrelated table/write operations before accepting the phase.

## Rotation and rollback

Change only the service's approved `PG_BACKUP_PASSWORD` field in 1Password. Keep the 64-character lowercase hexadecimal format required by the SQL. Publish a reviewed Job version change through GitOps so the immutable completed Job reruns, verify ESO sync and dedicated authentication, then update dependent capture identities. Never rotate an unrelated application's credential or place values in arguments, logs, source, or evidence.

A GitOps revert removes provisioning resources, not roles or data. Before application capture rollout these roles have no dependent movers, so rollback can retain unused read-only roles without touching application data. Role retirement or incumbent repository cleanup is a separate reviewed operation. Retain incumbent backup credentials, repositories, and recovery points until full recovery acceptance and its rollback window finish.
