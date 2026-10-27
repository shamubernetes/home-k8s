# SAB WAN guard

Outbound-only Python standard-library worker. Polls the gateway directly every 25 seconds. A fresh UDM observation must identify exactly one active wired uplink, with matching interface, WAN label, online status and alive flag. Both links being physically up is not enough. Observations older than 180 seconds, contradictory data and API errors are unknown.

On backup WAN, the guard pauses a previously running SAB queue. It resumes only a guard-owned pause after primary WAN stays healthy for 120 seconds. Unknown data, failed API calls, restarts, flaps and observation gaps over 60 seconds reset that timer. A queue already paused before guard intervention is never owned or resumed.

## Ownership and recovery

The PVC journal is atomically replaced and fsynced, including its parent directory. `pause_pending` is persisted before pause. Only an acknowledged pause followed by a paused queue read becomes `owned`. Before resume, ownership is consumed into `resume_pending`. A successful resume and running queue read return to `idle`.

A pending journal after a crash or uncertain API outcome deliberately stops automatic actions, rather than guessing or replaying resume. Check worker logs for `operator intervention required`. For recovery, stop the guard through GitOps under maintenance, inspect SAB state, decide the desired queue state manually, and reset the journal to `{"version":1,"state":"idle"}` on the retained PVC before restarting. Resetting to idle does not adopt an existing paused queue. Corrupt journals fail startup. Do not delete the PVC as a restart mechanism. It is protected from Flux pruning.

SAB exposes no pause reason or caller ownership token. A manual pause while the queue is already guard-paused is indistinguishable and cannot be observed. The journal remains authoritative in that case. An observed manual resume relinquishes ownership. This worker does not change SAB or UniFi upstream APIs.

The single replica uses Recreate and a Ceph RWO PVC. Do not scale it above one or run another guard against the same SAB instance. No ingress, Service, RBAC, service-account token, database or new build pipeline is needed. No HTTP health endpoint is invented. Process readiness alone does not prove WAN polling; inspect error and pending-state logs during rollout.

## Credentials and TLS

The ExternalSecret copies only the approved Unpoller username and password fields from the existing `unpoller` item. SAB uses the existing `sabnzbd-secret` API-key field. No credentials appear in code or logs. UniFi cookies and CSRF tokens stay in memory. Each HTTPS connection verifies the configured leaf certificate SHA256 before sending any HTTP bytes or credentials. The self-signed leaf therefore uses explicit pin authentication rather than public-CA validation. Certificate rotation requires a reviewed pin update. WAN identity is based on the established WAN/eth8 and WAN2/eth9 gateway contract, checked through matching active interface data.

## Validation and deployment handoff

```sh
python3 -B -m unittest discover -s kubernetes/apps/arrs/sab-wan-guard/tests -v
scripts/validate-app arrs/sab-wan-guard
scripts/check-image-pins kubernetes/apps/arrs/sab-wan-guard/app
```

Tests run real loopback HTTP fixture APIs, including login, cookies, CSRF, queue actions and error responses. A self-signed HTTPS fixture checks both certificate-pin rejection and successful authentication. No tests call production APIs or toggle WANs.

Deployment remains a separate parent-owned gate. Review the checked PR, establish maintenance, merge, observe the exact Flux revision, confirm ExternalSecret sync, PVC binding, one running worker and clean polling logs. Verify a fresh read-only WAN observation and SAB queue state. Do not force household WAN failover to prove the fixtures. Rollback stops the guard, preserves its PVC and leaves any queue pause for deliberate operator recovery. It never blindly resumes SAB.
