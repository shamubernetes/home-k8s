# Talos 1.14 rollout and configuration order

This change is a coordinated OS, etcd and configuration migration. It is not a
fleet-wide apply of newly generated configs.

## Entry gates

1. Complete the preceding Talos 1.13 patch rollout and verify every actual server,
   Kubernetes node and etcd member. Require all existing Ceph, VolSync, playback,
   volume-detach and maintenance gates. Do not suppress a health warning to proceed.
2. Retain encrypted, restore-tested etcd recovery evidence and the current effective
   per-node configs and Talos secrets. The 1.14 default upgrades etcd to 3.7; restoring
   the old OS slot alone is not a complete etcd downgrade plan.
3. Preserve the live Image Factory schematic and required hardware extensions.
   Verify the target installer manifest and complete exact-head CI.

## Canary, then fleet

The TalosUpgrade selector initially limits the OS upgrade to `k8s-rhea`. Keep the
selector until that worker has passed its functional checks. `parallelism: 1` alone
is not a manual canary stop and does not imply workers first.

Tuppr upgrades the OS using the node's currently applied config. It does not apply
Talhelper's newly generated configuration. Retain the legacy representation for
that OS upgrade, as supported by Talos 1.14.

Only after the actual server is on the target release:

1. Regenerate real configs using `task talos:render`. The task appends the CRI/NFS
   documents exactly once and validates every resulting config strictly. Direct
   `talhelper genconfig` does not perform those append steps.
2. Preview one exact node with `task talos:diff node=<IP> config=<private-file>`.
   Review networking, install image/disk, extensions, kubelet mounts, DNS, NFS, CRI,
   admission behavior and generated defaults. Generated configs contain secrets.
3. Apply through `task talos:apply` with an explicit mode inside maintenance.
   The server-version guard refuses older or ambiguous servers before diff/apply.
   A no-reboot apply can still restart CRI and disrupt workloads.
4. Verify the actual server version, kernel and extensions, node readiness, Cilium,
   mounted storage, application paths and Ceph. On control planes also verify all
   etcd members, API availability and unchanged admission behavior.
5. Expand the selector through a protected GitOps change for the next approved
   scope. Repeat the checks after each node. Remove the temporary selector only
   after the intended fleet rollout is complete; do not strand future upgrades on
   the canary.

## Preserved admission behavior

The old source deliberately removed `admissionControl`. Talhelper 1.14 otherwise
adds a default `KubeAdmissionControlConfig/PodSecurity` document. The append helper
removes only that exact generated default, preserves other admission/security
documents, and refuses an unexpected customized default. Changing admission policy
is separate work, not an implicit side effect of this upgrade.

The migration fixtures cover this preservation, duplicate-append refusal and
server-versus-client version checks. Talos 1.13 cannot decode several of the new
1.14 document kinds, even in staged apply mode.

## Tooling coordination

This coordinated change includes the operational talosctl and ARC/Kyverno pins.
The separate tooling-only update is superseded only after these changes merge and
verify. Do not relax strict validation of 1.13 configs just to install a 1.14 CLI.
