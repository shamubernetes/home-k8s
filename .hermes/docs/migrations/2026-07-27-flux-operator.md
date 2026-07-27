# Flux Operator migration, 2026-07-27

## Result

Production Flux was migrated in place from the legacy `Kustomization/flux` distribution to Flux Operator without loss or replacement of application resources.

Final repository revision: `4f3cc9447c830d7b181ef4bed813ef4abbfd4fda`

Final control plane:

- Flux Operator chart `0.57.0`, OCI digest `sha256:35105642acae3aceaf6a22e986653f766827c46e72179a9fec23214ec5f5317c`.
- Flux Operator image `v0.57.0@sha256:7baa2697d3c4d81007c02302c55a8601f02ad2052df5952b95f8a6708c7be131` on the cluster's `amd64` nodes.
- FluxInstance chart `0.57.0`, OCI digest `sha256:2ef25fdd727deeb2480ec563845961b13c7258a422570a15a010fec0b6384803`.
- Flux distribution `2.8.8` with the original six controllers: source, kustomize, helm, notification, image-reflector, and image-automation.
- No `spec.sync`, no source-watcher, no Operator-generated NetworkPolicies, and no Operator web UI.
- `GitRepository/home-kubernetes`, `Kustomization/cluster`, and `Kustomization/cluster-apps` retained their names and UIDs.
- `cluster` and `cluster-apps` use `prune: true` with `deletionPolicy: Orphan`.

## Guarded sequence used

1. Exported a redacted production inventory and rollback bundle outside the cluster.
2. Set `prune: false` and `deletionPolicy: Orphan` on `cluster`, `cluster-apps`, and the legacy `flux` Kustomization.
3. Installed Flux Operator idle and proved it did not alter the legacy six controllers or applications.
4. Built the proposed FluxInstance offline with the official Flux Operator CLI.
5. Patched controller flags, resources, pod-template metadata, notification CRD enums, and RBAC until the Operator render had zero non-metadata differences from the legacy 36-object distribution.
6. Added the FluxInstance while retaining the legacy distribution declaration.
7. Verified Operator adoption, then suspended the legacy `Kustomization/flux`.
8. Reconciled `cluster` and `cluster-apps` successfully while the legacy distribution was suspended.
9. Removed the legacy declaration from Git while parent pruning remained disabled.
10. Deleted the orphaned, suspended `Kustomization/flux`, verified all six controller Deployment UIDs were unchanged, then deleted `OCIRepository/flux-manifests`.
11. Reconciled the clean Operator-only state and restored normal root pruning.

## Exception encountered

The optional flux-instance chart health-check hook generated an invalid Kubernetes label when the CLI image tag included a required OCI digest suffix (`+sha`). The FluxInstance and all six controllers were already Ready, and application continuity checks were clean. The hook was disabled, the HelmRelease recovered to Ready, and FluxInstance readiness remained verified directly.

No iMessage incident alert was sent because there was no application, data, storage, routing, or reconciliation outage.

## Continuity evidence

Preflight bundle:

`/Volumes/Data/backups/maudebot/home-k8s/flux-operator-migration/20260727T190300Z-preflight`

The bundle contains the redacted root objects, inventories, controller resources, application pods, workload controllers, PVCs, HelmRelease status, Receiver state, pre-instance controller baseline, rendered diff, and final reports.

Verified outcomes:

- No baseline application pod UID changed.
- No application workload was rolled out by the migration.
- One existing Plane silo container restart occurred at `2026-07-27T19:50:58Z`. It was the pod's established hourly failure mode caused by an uncaught Redis socket closure. Prometheus showed six restarts in six hours and 24 in 24 hours; its pod UID and workload identity were unchanged.
- All 115 baseline PVC UIDs and PV bindings were unchanged.
- All 98 baseline HelmRelease UIDs and revisions were unchanged.
- No application Secret was created during the migration window. New Secrets were limited to Flux Operator entitlement metadata and Helm storage for the two new Flux releases.
- The `cluster` UID remains `4dec6d37-71cf-41be-b184-f0c7f66608cf`.
- The `cluster-apps` UID remains `b9840e91-2b67-4fc1-9457-e261c1aed2c3`.
- All six controller Deployment UIDs remained unchanged across adoption and legacy-object deletion.
- `Receiver/github-receiver` retained UID `60758f4f-56c9-4b1e-85b6-5263b4aa3df3` and still targets exactly `GitRepository/home-kubernetes`, `Kustomization/cluster`, and `Kustomization/cluster-apps`.
- `https://flux-webhook.thezoo.house` resolved through the expected route with valid TLS; an invalid hook path returned the expected HTTP 404.
- All Flux resources were Ready at the final revision.
- All five nodes were Ready, Ceph was `HEALTH_OK`, and all 169 PGs were active and clean.
- Controller logs contained zero error or fatal entries in the final 15-minute window.

The unrelated long-running `SmartDeviceInterfaceSlow` alert for `k8s-rhea/sda` remained present. Short-lived Renovate CPU throttling and etcd latency alerts were pending and unrelated to the Flux migration.

## Independent recovery path

The Operator and Instance chart versions and values are shared by GitOps and `talos/helmfile.yaml`. The Helmfile path renders without relying on live Flux.

Repair the existing Helm releases from outside Flux:

```bash
mise x helmfile@1.7.1 -- env KUBECONFIG="$HOME/.kube/config" \
  helmfile -f talos/helmfile.yaml --selector name=flux-operator sync
mise x helmfile@1.7.1 -- env KUBECONFIG="$HOME/.kube/config" \
  helmfile -f talos/helmfile.yaml --selector name=flux-instance sync
```

If both Operator reconciliation and the FluxInstance path are unusable:

1. Preserve a fresh object and inventory export.
2. Suspend the `flux-operator` and `flux-instance` HelmReleases so recovery is not immediately reverted.
3. Scale `Deployment/flux-operator` to zero.
4. Restore the `flux-system/sops-age` Secret from the approved secret store if it is absent.
5. Apply the retained legacy distribution without pruning applications:

```bash
kubectl apply --server-side -k kubernetes/bootstrap/flux
```

6. Restore the exported `home-kubernetes`, `cluster`, and `cluster-apps` objects from the preflight bundle if they are absent.
7. Verify Sources, Kustomizations, HelmReleases, Receiver targets, application pods, Secrets, and PVC/PV bindings before making any further cleanup.

Do not delete the FluxInstance or uninstall either Helm release as an initial recovery action. Preserve application resources and root identities first.

## Rehearsal decision

A disposable-cluster takeover and failed-takeover rehearsal was proposed but explicitly declined. The accepted replacement was a guarded production-first sequence with full offline render parity, three-level orphan protection, independent Helmfile recovery artifacts, stepwise PRs, live identity checks after every ownership transition, and retained legacy bootstrap manifests.
