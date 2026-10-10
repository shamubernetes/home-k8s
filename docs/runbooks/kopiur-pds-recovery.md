# PDS whole-store recovery qualification

This candidate is a PDS-only remote qualification lane for K8S-92. It does not deploy a recurring capture, change the source application, or retire VolSync. The original application and shared-service denominator remains unchanged.

## Exact boundary

The source contract is services/atproto-pds, Deployment atproto-pds, PVC atproto-pds mounted at /pds. Capture the entire store, including actor databases and signing keys, account/session and sequencer databases, all SQLite sidecars, repository blocks, blob bytes and empty directories. Runtime configuration and authentication inputs must come from the existing application escrow. A database export or a sample repository CAR alone is not a complete generation.

The remote fixture uses the exact image digest declared in the current HelmRelease and UID/GID 1000. It creates two accounts through the pinned native application libraries, repositories, signing keys, sessions, records and blobs. The source stops only long enough to establish and verify a private immutable whole-volume Docker copy. It resumes before either encrypted upload. This is a non-production capture boundary, not proof of live Kubernetes CSI timing or source failback.

The exact production Kopiur mover uploads the same immutable point to the service-scoped real NAS share and R2 bucket, under a new UUID fixture repository. Both producers and the original/immutable-point volumes are removed before independent fresh-context restores. Wrong encryption passwords must fail for each backend. The complete restored inventory must match bytes, modes and owners before boot. Every nested SQLite database and both actor keys are checked.

Both recovered copies use the unmodified production image entrypoint with blocked egress. Native checks cover account login, original refresh sessions, original record CIDs/values, signed new writes, repository CAR reads and blob bytes. The restore path cannot run the fixture seed callback. Receipts expose only counts, point IDs, manifest hashes, timing and qualified image digests.

## Credentials and execution

The dedicated Kubernetes vault item is kopiur-atproto-pds. NAS_USERNAME is kp-atproto-pds, NAS_SHARE and R2_BUCKET are kopiur-atproto-pds. The service identity has a private non-guest NAS share and one-bucket R2 object policy. Existing repository encryption fields and incumbent roots are retained. Do not use guest or volsync-template credentials, rotate encryption passwords, or recreate historical repositories.

Publish the reviewed branch tip, then dispatch the existing trusted-main workflow:

    gh workflow run recovery-verify.yaml --repo shamubernetes/home-k8s --ref main -f suite=identities -f commit=<exact-published-head>

This candidate's private identity channel accepts only atproto-pds. Verify the dispatch inputs, successful exact checkout step, allocated ghar-set-zoo runner and Pod UID before sending the approved vault fields through stdin to its Unix socket. Never put credentials in GitHub inputs, argv, fixture logs or disk files. The job has no Kubernetes credentials. Do not run this fixture or repository validators on the Mac.

Each receipt includes actual NAS/R2 snapshot and root object IDs. Fixture cleanup verifies and purges only its UUID-owned fixture paths and removes its labelled containers/volumes. Retained production recovery points are not fixture cleanup targets.

## Remaining deployment gates

The PDS-only suspended capture assets are a separate candidate. They are not wired into the application kustomization. The worker records the source PVC UID, resourceVersion and PV name, plus the CSI snapshot UID and content binding. It resumes the source before NAS upload and writes an immutable generation receipt. A successor may replace the completed lock only after the previous worker/watchdog Jobs are terminal and their pods have stopped, preserving the previous receipt and using resourceVersion compare-and-swap. Failed or abandoned captures require explicit recovery. No automatic timeout lock stealing or recovery-point deletion is implemented.

The trusted identities suite runs receipt-contract regressions and the actual worker/watchdog processes against an API fixture with a disposable Docker SQLite writer. CSI and Kopiur responses in these orchestration tests are simulated. The existing independent native PDS NAS/R2 drill remains the provider qualification. Neither test substitutes for live CSI lineage, complete production escrow, retained-point recovery, repeatable retention/cleanup or R2 replication completion. A capture's complete marker means the configured source set reached NAS, not full migration or R2 acceptance.

Render isolated Restore resources with scripts/k8s92_infrastructure_restore.py atproto-pds --receipt <immutable-receipt.json> --namespace <pds-recovery-disposable> --tier nas-smb --output <new-output.json>. The renderer refuses mutable locks, wrong application/source namespace, partial or duplicated sources, missing PVC/CSI lineage, changed snapshot generation, malformed IDs and native identity mismatches. It preserves owner/mode restoration and writes a new output file exclusively. It does not call Kubernetes or boot the restored application.

An R2 render additionally needs --r2-ids <verified-destination-map.json>. Obtain this map from independent destination catalog verification, never from the capture worker or a latest-snapshot guess. It must contain run, namespace, repository and points. For atproto-pds the repository is atproto-pds-r2, namespace is services, and points.atproto-pds contains sourceNASID and snapshotID. The generation and source NAS ID must match the immutable receipt. These checks prevent accidental cross-generation selection, but do not authenticate a forged input document or replace destination-native verification.

These remote results must not be relabelled as live source or production migration acceptance. The integration phase still owns a bounded repeatable Kubernetes capture/watchdog lifecycle, true CSI and source-PVC lineage, recurring transport/retention policies, original application config/key escrow qualification, direct retained-legacy-point restores, protected admission/render checks, source outage/failback proof and functional production health. Keep capture suspended and all incumbent VolSync/recovery points until those gates pass. A descriptive contract does not create a Repository, SnapshotPolicy or Restore resource.
