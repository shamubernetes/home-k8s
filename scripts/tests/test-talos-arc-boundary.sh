#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

tmpdir=$(mktemp -d)
fixture_dir=kubernetes/apps/hermes-talos-boundary-fixture
trap 'rm -rf "$tmpdir" "$fixture_dir"' EXIT

values="$tmpdir/values.yaml"
rendered="$tmpdir/rendered.yaml"
chart_source=kubernetes/apps/actions-runner-system/ghar-scale-set/arc-talos/ocirepository.yaml
chart_url=$(yq -r '.spec.url' "$chart_source")
chart_version=$(yq -r '.spec.ref.tag' "$chart_source")
[[ $chart_url == oci://* && -n $chart_version && $chart_version != null ]]
yq -o=yaml '.spec.values' \
  kubernetes/apps/actions-runner-system/ghar-scale-set/arc-talos/helmrelease.yaml > "$values"
helm template ghar-set-talos \
  "$chart_url" \
  --version "$chart_version" \
  --namespace actions-runner-talos \
  -f "$values" > "$rendered"

[[ $(yq -r 'select(.kind == "AutoscalingRunnerSet") | .metadata.name' "$rendered" | grep -c '^ghar-set-talos$') -eq 1 ]]
yq -e 'select(.kind == "AutoscalingRunnerSet") |
  .metadata.name == "ghar-set-talos" and
  .metadata.namespace == "actions-runner-talos" and
  .spec.runnerGroup == "talos-image-pull" and
  .spec.maxRunners == 1 and
  .spec.template.spec.serviceAccountName == "ghar-set-talos-gha-rs-no-permission" and
  .spec.template.spec.automountServiceAccountToken == false and
  .spec.template.spec.securityContext.runAsUser == 1001 and
  .spec.template.spec.securityContext.runAsGroup == 123 and
  .spec.template.spec.securityContext.fsGroup == 123 and
  ([.spec.template.spec.volumes[] | select(.name == "talosconfig" and .secret.secretName == "ghar-set-talos-talosconfig" and .secret.defaultMode == 288)] | length == 1) and
  ([.spec.template.spec.containers[] | select(.name == "runner" and .securityContext.runAsNonRoot == true and .securityContext.allowPrivilegeEscalation == false)] | length == 1) and
  ([.spec.template.spec.volumes[] | select(.name == "talosctl-source") | .image.reference | select(test("@sha256:[0-9a-f]{64}$"))] | length == 1)' \
  "$rendered" >/dev/null

runner_image=$(yq -r '.spec.values.template.spec.containers[] | select(.name == "runner") | .image' \
  kubernetes/apps/actions-runner-system/ghar-scale-set/arc-talos/helmrelease.yaml)
init_runner_image=$(yq -r '.spec.values.template.spec.initContainers[] | select(.name == "install-talosctl") | .image' \
  kubernetes/apps/actions-runner-system/ghar-scale-set/arc-talos/helmrelease.yaml)
talosctl_image=$(yq -r '.spec.values.template.spec.volumes[] | select(.name == "talosctl-source") | .image.reference' \
  kubernetes/apps/actions-runner-system/ghar-scale-set/arc-talos/helmrelease.yaml)
[[ $init_runner_image == "$runner_image" ]]
[[ $(yq -r '.spec.rules[] | select(.name == "pin-trusted-runner-images-and-tools") |
  .validate.pattern.spec.containers[] | select(.["(name)"] == "runner") | .image' \
  kubernetes/apps/kyverno/kyverno/policies/talos-runner-boundary.yaml) == "$runner_image" ]]
[[ $(yq -r '.spec.rules[] | select(.name == "pin-trusted-runner-images-and-tools") |
  .validate.pattern.spec.initContainers[] | select(.["(name)"] == "install-talosctl") | .image' \
  kubernetes/apps/kyverno/kyverno/policies/talos-runner-boundary.yaml) == "$runner_image" ]]
[[ $(yq -r '.spec.rules[] | select(.name == "pin-trusted-runner-images-and-tools") |
  .validate.pattern.spec.volumes[] | select(.["(name)"] == "talosctl-source") | .image.reference' \
  kubernetes/apps/kyverno/kyverno/policies/talos-runner-boundary.yaml) == "$talosctl_image" ]]

yq -e '.kind == "NetworkPolicy" and
  (.spec.podSelector | length) == 0 and
  (.spec.ingress | length) == 0 and
  (.spec.egress | length) == 0 and
  (.spec.policyTypes | length) == 2 and
  .spec.policyTypes[0] == "Ingress" and
  .spec.policyTypes[1] == "Egress"' \
  kubernetes/apps/actions-runner-system/ghar-scale-set/arc-talos/networkpolicy-default-deny.yaml >/dev/null
yq -e '.spec.endpointSelector.matchLabels."actions.github.com/scale-set-name" == "ghar-set-talos"' \
  kubernetes/apps/actions-runner-system/ghar-scale-set/arc-talos/networkpolicy.yaml >/dev/null
yq -e '.spec.endpointSelector.matchLabels."actions.github.com/scale-set-name" == "ghar-set-maudecode" and (.spec.egressDeny | length) == 2' \
  kubernetes/apps/actions-runner-system/ghar-scale-set/arc-maudecode/networkpolicy.yaml >/dev/null
yq -e '.spec.endpointSelector.matchLabels."actions.github.com/scale-set-name" == "ghar-set-zoo" and (.spec.egressDeny | length) == 2' \
  kubernetes/apps/actions-runner-system/ghar-scale-set/arc-zoo/networkpolicy.yaml >/dev/null

policy=kubernetes/apps/kyverno/kyverno/policies/talos-runner-boundary.yaml
cat > "$tmpdir/arc-userinfo.yaml" <<'YAML'
roles: []
clusterRoles: []
userInfo:
  username: system:serviceaccount:actions-runner-system:ghar-controller
  groups:
  - system:serviceaccounts
  - system:serviceaccounts:actions-runner-system
YAML
cat > "$tmpdir/flux-userinfo.yaml" <<'YAML'
roles: []
clusterRoles: []
userInfo:
  username: system:serviceaccount:flux-system:kustomize-controller
  groups:
  - system:serviceaccounts
  - system:serviceaccounts:flux-system
YAML
cat > "$tmpdir/runner-pod.yaml" <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: ghar-set-talos-test-runner
  namespace: actions-runner-talos
  labels:
    actions.github.com/scale-set-name: ghar-set-talos
  ownerReferences:
  - apiVersion: actions.github.com/v1alpha1
    kind: EphemeralRunner
    name: ghar-set-talos-test-runner
    uid: 11111111-1111-1111-1111-111111111111
    controller: true
spec:
  automountServiceAccountToken: false
  serviceAccountName: ghar-set-talos-gha-rs-no-permission
  restartPolicy: Never
  securityContext:
    runAsUser: 1001
    runAsGroup: 123
    fsGroup: 123
    seccompProfile:
      type: RuntimeDefault
  containers:
  - name: runner
    image: fixture.invalid/runner:replace-me
    command:
    - /home/runner/run.sh
    env:
    - name: ACTIONS_RUNNER_REQUIRE_JOB_CONTAINER
      value: "false"
    - name: ACTIONS_RUNNER_INPUT_JITCONFIG
      valueFrom:
        secretKeyRef:
          key: jitToken
          name: ghar-set-talos-test-runner
    - name: GITHUB_ACTIONS_RUNNER_EXTRA_USER_AGENT
      value: actions-runner-controller/0.14.2
    - name: ACTIONS_RUNNER_RETURN_VERSION_DEPRECATED_EXIT_CODE
      value: "1"
    securityContext:
      runAsNonRoot: true
      allowPrivilegeEscalation: false
      capabilities:
        drop:
        - ALL
    volumeMounts:
    - name: talosconfig
      mountPath: /var/run/secrets/talos.dev
      readOnly: true
    - name: talosctl
      mountPath: /opt/talosctl
      readOnly: true
  initContainers:
  - name: install-talosctl
    image: fixture.invalid/runner:replace-me
    command:
    - /bin/bash
    - -ceu
    args:
    - install -m 0555 /source/talosctl /tools/talosctl
    securityContext:
      runAsNonRoot: true
      allowPrivilegeEscalation: false
      capabilities:
        drop:
        - ALL
    volumeMounts:
    - name: talosctl-source
      mountPath: /source
      readOnly: true
    - name: talosctl
      mountPath: /tools
  volumes:
  - name: talosconfig
    secret:
      secretName: ghar-set-talos-talosconfig
      defaultMode: 288
  - name: talosctl
    emptyDir: {}
  - name: talosctl-source
    image:
      reference: fixture.invalid/talosctl:replace-me
      pullPolicy: IfNotPresent
YAML
RUNNER_IMAGE="$runner_image" TALOSCTL_IMAGE="$talosctl_image" yq -i '
  (.spec.containers[] | select(.name == "runner") | .image) = strenv(RUNNER_IMAGE) |
  (.spec.initContainers[] | select(.name == "install-talosctl") | .image) = strenv(RUNNER_IMAGE) |
  (.spec.volumes[] | select(.name == "talosctl-source") | .image.reference) = strenv(TALOSCTL_IMAGE)
' "$tmpdir/runner-pod.yaml"
cat > "$tmpdir/admin-serviceaccount.yaml" <<'YAML'
apiVersion: talos.dev/v1alpha1
kind: ServiceAccount
metadata:
  name: ghar-set-talos-talosconfig
  namespace: actions-runner-talos
spec:
  roles:
  - os:admin
YAML
cat > "$tmpdir/deployment.yaml" <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: forbidden
  namespace: actions-runner-talos
spec:
  selector:
    matchLabels:
      app: forbidden
  template:
    metadata:
      labels:
        app: forbidden
    spec:
      containers:
      - name: forbidden
        image: registry.k8s.io/pause@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
YAML

kyverno apply "$policy" --resource "$tmpdir/runner-pod.yaml" \
  --userinfo "$tmpdir/arc-userinfo.yaml" --remove-color

yq '.spec.containers[0].image = "registry.k8s.io/pause@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"' \
  "$tmpdir/runner-pod.yaml" > "$tmpdir/untrusted-image-pod.yaml"
yq '.spec.containers += [{"name": "sidecar", "image": "registry.k8s.io/pause@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]' \
  "$tmpdir/runner-pod.yaml" > "$tmpdir/sidecar-pod.yaml"
for fixture in untrusted-image-pod.yaml sidecar-pod.yaml; do
  if kyverno apply "$policy" --resource "$tmpdir/$fixture" \
      --userinfo "$tmpdir/arc-userinfo.yaml" --remove-color >/dev/null 2>&1; then
    echo "ARC-shape bypass fixture unexpectedly passed: $fixture" >&2
    exit 1
  fi
done

for fixture in runner-pod.yaml admin-serviceaccount.yaml deployment.yaml; do
  if kyverno apply "$policy" --resource "$tmpdir/$fixture" \
      --userinfo "$tmpdir/flux-userinfo.yaml" --remove-color >/dev/null 2>&1; then
    printf 'Kyverno accepted forbidden fixture: %s\n' "$fixture" >&2
    exit 1
  fi
done

scripts/check-talos-runner-boundary
mkdir -p "$fixture_dir"
cat > "$fixture_dir/forbidden.yaml" <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: forbidden
  namespace: actions-runner-talos
spec:
  containers:
  - name: forbidden
    image: registry.k8s.io/pause@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
YAML
if scripts/check-talos-runner-boundary >/dev/null 2>&1; then
  echo 'source ownership guard accepted a resource outside the owned path' >&2
  exit 1
fi
rm -rf "$fixture_dir"
printf 'ok: rendered ARC, admission, role, source ownership, and network boundaries\n'
