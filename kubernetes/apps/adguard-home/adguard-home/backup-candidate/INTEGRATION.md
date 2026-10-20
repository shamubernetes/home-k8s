# Executable integration handoff

This root owns Kopiur protection for the primary `data-adguard-home-0` PVC.
Registration initializes the dedicated repositories and requests one retained
original. The policy, daily schedule and replication stay suspended until original
recovery acceptance. Production PodTemplates and incumbent VolSync stay unchanged.
Backend ExternalSecrets use the existing dedicated item. Require actual ESO sync,
repository readiness and the retained original before claiming capture success.
Root movers match the observed native owner UID 0. Do not set fsGroup, because
that would change staging-clone metadata. Both repository defaults use UTC.
The explicit daily slots are 22:00 and 22:20 UTC, initially suspended. Enable them
only after original acceptance and observe their natural same-point execution.

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

## Native restore execution

`verify.py` hashes the complete restored conf/work trees and runs the actual
AdGuard configuration validator on a separate disposable copy. Version 0.107.79
can rewrite files even under `--check-config`; running it on the accepted original
receipt tree is unsafe. A matching-version native binary booted the fixture and
answered the deterministic DNS rewrite again after an independent tree copy.
The macOS release test is not proof of the production Linux image or original
NAS/R2 history. No production process was stopped.

On each independent original restore, mount the original tree read-only and run:

```sh
python backup-candidate/verify.py --restore-root /restore \
  --binary /native/AdGuardHome --receipt /private/adguard-original.json
```

Then boot another writable copy with the exact deployed Linux image, loopback-only
DNS and HTTP bindings, upstream/device egress blocked. Compare native statistics,
sessions, filters and deterministic DNS responses. Keep the untouched receipt
copy. The primary policy does not assert replica-local query-history equality.
