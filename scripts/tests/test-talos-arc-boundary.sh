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

policy=kubernetes/apps/kyverno/kyverno/policies/talos-runner-boundary-cel.yaml
[[ $(yq -r 'select(.metadata.name == "restrict-talos-runner-pods") | .spec.variables[] | select(.name == "runnerImage") | .expression' "$policy") == "'$runner_image'" ]]
[[ $(yq -r 'select(.metadata.name == "restrict-talos-runner-pods") | .spec.variables[] | select(.name == "talosImage") | .expression' "$policy") == "'$talosctl_image'" ]]
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

# A loader, compile, or evaluation error must never count as a policy denial.
assert_policy_result() {
  local expected=$1 resource=$2 user=$3 result="$tmpdir/result.log" status=0
  local values_file="$tmpdir/${4:-create}-values.yaml"
  kyverno apply "$policy" --resource "$resource" --values-file "$values_file" \
    --userinfo "$user" --remove-color > "$result" 2>&1 || status=$?
  if [[ $expected == pass ]]; then
    if [[ $status != 0 ]] || ! grep -Eq 'pass: 1, fail: 0, warn: 0, error: 0, skip: 0' "$result"; then
      cat "$result" >&2
      return 1
    fi
  else
    if [[ $status != 1 ]] || ! grep -Eq 'fail: [1-9][0-9]*, warn: 0, error: 0, skip: 0' "$result"; then
      cat "$result" >&2
      echo "Expected an evaluated policy denial: $resource" >&2
      return 1
    fi
  fi
}
cat > "$tmpdir/create-values.yaml" <<'YAML'
apiVersion: cli.kyverno.io/v1alpha1
kind: Values
globalValues:
  request.operation: CREATE
YAML
cat > "$tmpdir/update-values.yaml" <<'YAML'
apiVersion: cli.kyverno.io/v1alpha1
kind: Values
globalValues:
  request.operation: UPDATE
YAML
cat > "$tmpdir/ephemeral-values.yaml" <<'YAML'
apiVersion: cli.kyverno.io/v1alpha1
kind: Values
globalValues:
  request.operation: UPDATE
subresources:
- subresource:
    name: pods/ephemeralcontainers
    group: ""
    version: v1
    kind: Pod
  parentResource:
    name: pods
    group: ""
    version: v1
    kind: Pod
YAML
assert_policy_result pass "$tmpdir/runner-pod.yaml" "$tmpdir/arc-userinfo.yaml"
assert_policy_result fail "$tmpdir/runner-pod.yaml" "$tmpdir/arc-userinfo.yaml" ephemeral

yq '.spec.containers[0].image = "registry.k8s.io/pause@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"' \
  "$tmpdir/runner-pod.yaml" > "$tmpdir/untrusted-image-pod.yaml"
yq '.spec.containers += [{"name": "sidecar", "image": "registry.k8s.io/pause@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]' \
  "$tmpdir/runner-pod.yaml" > "$tmpdir/sidecar-pod.yaml"
for fixture in untrusted-image-pod.yaml sidecar-pod.yaml; do
  assert_policy_result fail "$tmpdir/$fixture" "$tmpdir/arc-userinfo.yaml"
done

for fixture in runner-pod.yaml admin-serviceaccount.yaml deployment.yaml; do
  assert_policy_result fail "$tmpdir/$fixture" "$tmpdir/flux-userinfo.yaml"
done

# Exercise each translated Pod restriction independently against the valid shape.
mutations=(
  '.spec.initContainers[0].image = "fixture.invalid/untrusted:latest"'
  '.spec.volumes[2].image.reference = "fixture.invalid/untrusted:latest"'
  '.metadata.labels."actions.github.com/scale-set-name" = "other"'
  '.metadata.ownerReferences[0].kind = "Other"'
  '.metadata.ownerReferences[0].controller = false'
  '.spec.automountServiceAccountToken = true'
  '.spec.serviceAccountName = "default"'
  '.spec.securityContext.runAsUser = 0'
  '.spec.securityContext.runAsGroup = 0'
  '.spec.securityContext.fsGroup = 0'
  '.spec.securityContext.seccompProfile.type = "Unconfined"'
  '.spec.containers[0].securityContext.allowPrivilegeEscalation = true'
  '.spec.containers[0].securityContext.runAsNonRoot = false'
  '.spec.containers[0].securityContext.capabilities.drop = []'
  '.spec.containers[0].command = ["/bin/sh"]'
  '.spec.containers[0].command += ["extra"]'
  '.spec.containers[0].volumeMounts[0].readOnly = false'
  '.spec.containers[0].volumeMounts[0].mountPath = "/other"'
  '.spec.containers[0].volumeMounts[1].readOnly = false'
  '.spec.containers[0].volumeMounts += [{"name": "extra", "mountPath": "/extra"}]'
  '.spec.initContainers[0].securityContext.allowPrivilegeEscalation = true'
  '.spec.initContainers[0].securityContext.runAsNonRoot = false'
  '.spec.initContainers[0].securityContext.capabilities.drop = []'
  '.spec.initContainers[0].command = ["/bin/sh", "-ceu"]'
  '.spec.initContainers[0].args = ["echo bypass"]'
  '.spec.initContainers[0].volumeMounts[0].readOnly = false'
  '.spec.initContainers[0].volumeMounts[1].mountPath = "/other"'
  '.spec.initContainers += [.spec.initContainers[0] | .name = "extra"]'
  '.spec.volumes[0].secret.secretName = "other"'
  '.spec.volumes[0].secret.defaultMode = 420'
  '.spec.volumes[1] = {"name": "talosctl", "hostPath": {"path": "/"}}'
  '.spec.volumes[2].image.pullPolicy = "Always"'
  '.spec.volumes += [{"name": "extra", "emptyDir": {}}]'
)
for mutation in "${mutations[@]}"; do
  yq "$mutation" "$tmpdir/runner-pod.yaml" > "$tmpdir/mutated-pod.yaml"
  assert_policy_result fail "$tmpdir/mutated-pod.yaml" "$tmpdir/arc-userinfo.yaml"
done

yq '.spec.roles = ["os:operator"]' "$tmpdir/admin-serviceaccount.yaml" > "$tmpdir/operator-serviceaccount.yaml"
assert_policy_result pass "$tmpdir/operator-serviceaccount.yaml" "$tmpdir/flux-userinfo.yaml"
assert_policy_result pass "$tmpdir/operator-serviceaccount.yaml" "$tmpdir/flux-userinfo.yaml" update
assert_policy_result fail "$tmpdir/admin-serviceaccount.yaml" "$tmpdir/flux-userinfo.yaml" update
assert_policy_result fail "$tmpdir/deployment.yaml" "$tmpdir/flux-userinfo.yaml" update
for mutation in '.metadata.name = "other"' '.spec.roles += ["os:admin"]' '.spec.roles = []'; do
  yq "$mutation" "$tmpdir/operator-serviceaccount.yaml" > "$tmpdir/mutated-serviceaccount.yaml"
  assert_policy_result fail "$tmpdir/mutated-serviceaccount.yaml" "$tmpdir/flux-userinfo.yaml"
done
for identity in 'apps/v1 DaemonSet' 'apps/v1 ReplicaSet' 'apps/v1 StatefulSet' 'batch/v1 Job' 'batch/v1 CronJob' 'v1 ReplicationController'; do
  read -r api kind <<< "$identity"
  API="$api" KIND="$kind" yq '.apiVersion = strenv(API) | .kind = strenv(KIND)' \
    "$tmpdir/deployment.yaml" > "$tmpdir/controller.yaml"
  assert_policy_result fail "$tmpdir/controller.yaml" "$tmpdir/flux-userinfo.yaml"
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
