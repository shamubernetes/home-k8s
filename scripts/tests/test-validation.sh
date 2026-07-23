#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
fixtures="${repo_root}/scripts/tests/fixtures"
resolver="${repo_root}/scripts/resolve-helmrelease-chart"
values_resolver="${repo_root}/scripts/resolve-helmrelease-values"
image_check="${repo_root}/scripts/check-image-pins"
document_check="${repo_root}/scripts/check-kubernetes-documents"
postrender="${repo_root}/scripts/apply-helmrelease-postrenderers"
runtime_resolver="${repo_root}/scripts/resolve-helmrelease-runtime"
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
helmreleases="${tmpdir}/helmreleases.yaml"
source_url=$(git remote get-url origin)
SOURCE_URL="$source_url" yq '
  (select(.kind == "GitRepository" and .metadata.name == "home-kubernetes").spec.url) = strenv(SOURCE_URL)
' "$fixtures/helmreleases.yaml" > "$helmreleases"

assert_eq() {
  local expected=$1 actual=$2 label=$3
  if [[ $actual != "$expected" ]]; then
    printf 'FAIL %s\nexpected: %q\nactual:   %q\n' "$label" "$expected" "$actual" >&2
    exit 1
  fi
}

actual=$($resolver "$helmreleases" inline tools)
assert_eq $'app-template\t4.6.2\thttps://bjw-s-labs.github.io/helm-charts' "$actual" 'inline HelmRepository chart'
IFS=$'\t' read -r inline_chart inline_version inline_repo <<< "$actual"
helm template inline-test "$inline_chart" --version "$inline_version" --repo "$inline_repo" > "${tmpdir}/inline-render.yaml"
$document_check "${tmpdir}/inline-render.yaml"

actual=$($resolver "$helmreleases" tagged tools)
assert_eq $'oci://docker.io/envoyproxy/gateway-helm\t1.8.1\t' "$actual" 'tagged OCIRepository chartRef'
IFS=$'\t' read -r tagged_chart tagged_version _ <<< "$actual"
helm template tagged-test "$tagged_chart" --version "$tagged_version" > "${tmpdir}/tagged-unpinned.yaml"
if $image_check "${tmpdir}/tagged-unpinned.yaml" >/dev/null 2>&1; then
  echo 'FAIL Helm-rendered unpinned chart image was accepted' >&2
  exit 1
fi
helm template tagged-test "$tagged_chart" --version "$tagged_version" \
  --set deployment.envoyGateway.image.repository=docker.io/envoyproxy/gateway \
  --set-string 'deployment.envoyGateway.image.tag=v1.8.1@sha256:497df13b71f4e544c7e80414873041e291776c28cd788bcbee0d18421fa5db98' \
  > "${tmpdir}/tagged-pinned.yaml"
$image_check "${tmpdir}/tagged-pinned.yaml"

actual=$($resolver "$helmreleases" digested tools)
assert_eq $'oci://ghcr.io/maudecode/charts/cowbell@sha256:ec37f48d0b97617c4997cab64779ae9ff8f5c8af7f22eeb3d9a91998cb570fe4\t\t' "$actual" 'digest OCIRepository chartRef'

actual=$($resolver "$helmreleases" semver tools)
assert_eq $'oci://ghcr.io/rafaribe/homelab-assistant-crds\t0.2.x\t' "$actual" 'semver OCIRepository chartRef'

actual=$($resolver "$helmreleases" local-git tools)
local_chart="${repo_root}/kubernetes/apps/tools/firecrawl/app/chart"
assert_eq "${local_chart}"$'\t\t' "$actual" 'current-checkout GitRepository chart'
helm template local-git "$local_chart" --skip-tests > "${tmpdir}/local-git-render.yaml"
$document_check "${tmpdir}/local-git-render.yaml"

RELEASE_NAME=local-git yq 'select(.kind == "HelmRelease" and .metadata.name == strenv(RELEASE_NAME))' \
  "$helmreleases" > "${tmpdir}/local-git-hr.yaml"
actual=$($runtime_resolver "${tmpdir}/local-git-hr.yaml" fallback)
assert_eq $'tools\trendered-tools\ttrue' "$actual" 'release, target, and test runtime settings'
yq '(select(.kind == "GitRepository" and .metadata.name == "home-kubernetes").spec.url) = "https://example.invalid/other/repository"' \
  "$helmreleases" > "${tmpdir}/external-git.yaml"
if $resolver "${tmpdir}/external-git.yaml" local-git tools >/dev/null 2>&1; then
  echo 'FAIL external GitRepository was rendered from the current checkout' >&2
  exit 1
fi
yq 'select(.kind != "GitRepository" or .metadata.name != "home-kubernetes")' \
  "$helmreleases" > "${tmpdir}/missing-git.yaml"
if $resolver "${tmpdir}/missing-git.yaml" local-git tools >/dev/null 2>&1; then
  echo 'FAIL missing GitRepository source was accepted' >&2
  exit 1
fi
yq '
  (select(.kind == "GitRepository" and .metadata.name == "home-kubernetes").metadata.name) = "other-checkout" |
  (select(.kind == "HelmRelease" and .metadata.name == "local-git").spec.chart.spec.sourceRef.name) = "other-checkout"
' "$helmreleases" > "${tmpdir}/wrong-git-identity.yaml"
if $resolver "${tmpdir}/wrong-git-identity.yaml" local-git tools >/dev/null 2>&1; then
  echo 'FAIL noncanonical GitRepository identity was accepted as the current checkout' >&2
  exit 1
fi
yq '
  (select(.kind == "GitRepository" and .metadata.name == "home-kubernetes").spec.ref) =
    {"commit": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"}
' "$helmreleases" > "${tmpdir}/wrong-git-ref.yaml"
if $resolver "${tmpdir}/wrong-git-ref.yaml" local-git tools >/dev/null 2>&1; then
  echo 'FAIL GitRepository ref that does not track main was accepted as the current checkout' >&2
  exit 1
fi

# chartRef sources must resolve in the HelmRelease's effective namespace.
yq '(select(.kind == "OCIRepository" and .metadata.name == "tagged").metadata.namespace) = "other"' \
  "$helmreleases" > "${tmpdir}/cross-namespace.yaml"
if $resolver "${tmpdir}/cross-namespace.yaml" tagged tools >/dev/null 2>&1; then
  echo 'FAIL cross-namespace OCIRepository chartRef was accepted' >&2
  exit 1
fi

$image_check "$fixtures/images-pinned.yaml"
$image_check "$fixtures/disabled-values.yaml"
if $image_check "$fixtures/images-unpinned.yaml" >/dev/null 2>&1; then
  echo 'FAIL unpinned init-container image was accepted' >&2
  exit 1
fi

cat > "${tmpdir}/postrender-hr.yaml" <<'YAML'
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: postrender-test
spec:
  targetNamespace: rendered-tools
  postRenderers:
  - kustomize:
      patches:
      - target:
          kind: Deployment
          name: pinned
          namespace: rendered-tools
        patch: |-
          apiVersion: apps/v1
          kind: Deployment
          metadata:
            name: pinned
          spec:
            template:
              spec:
                initContainers:
                - name: injected
                  image: docker.io/example/injected:latest
YAML
yq 'select(.kind == "Deployment") | .metadata.namespace = "rendered-tools"' \
  "$fixtures/images-pinned.yaml" > "${tmpdir}/targeted-render.yaml"
$postrender "${tmpdir}/postrender-hr.yaml" "${tmpdir}/targeted-render.yaml" "${tmpdir}/postrendered.yaml"
if $image_check "${tmpdir}/postrendered.yaml" >/dev/null 2>&1; then
  echo 'FAIL target-namespace post-renderer injected image was not validated' >&2
  exit 1
fi

cat > "${tmpdir}/postrender-images-hr.yaml" <<'YAML'
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: postrender-images-test
spec:
  postRenderers:
  - kustomize:
      images:
      - name: ghcr.io/example/app
        newTag: latest
YAML
$postrender "${tmpdir}/postrender-images-hr.yaml" "$fixtures/images-pinned.yaml" "${tmpdir}/postrendered-images.yaml"
assert_eq 'ghcr.io/example/app:latest' \
  "$(yq -r 'select(.kind == "Deployment") | .spec.template.spec.containers[] | select(.name == "app") | .image' "${tmpdir}/postrendered-images.yaml")" \
  'post-renderer image transform'
if $image_check "${tmpdir}/postrendered-images.yaml" >/dev/null 2>&1; then
  echo 'FAIL post-renderer image transform bypassed digest validation' >&2
  exit 1
fi

test_chart="${tmpdir}/test-hook-chart"
mkdir -p "${test_chart}/templates"
cat > "${test_chart}/Chart.yaml" <<'YAML'
apiVersion: v2
name: test-hook-chart
version: 0.1.0
YAML
cat > "${test_chart}/templates/test.yaml" <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: test-hook
  annotations:
    helm.sh/hook: test
spec:
  restartPolicy: Never
  containers:
  - name: test
    image: docker.io/example/test:latest
YAML
$runtime_resolver "${tmpdir}/local-git-hr.yaml" fallback > "${tmpdir}/enabled-test-runtime"
IFS=$'\t' read -r _ enabled_target enabled_tests < "${tmpdir}/enabled-test-runtime"
enabled_args=(template test-hook "$test_chart" -n "$enabled_target")
[[ $enabled_tests == true ]] || enabled_args+=(--skip-tests)
helm "${enabled_args[@]}" > "${tmpdir}/enabled-test-hook.yaml"
if $image_check "${tmpdir}/enabled-test-hook.yaml" >/dev/null 2>&1; then
  echo 'FAIL enabled Helm test hook image bypassed digest validation' >&2
  exit 1
fi
yq '.spec.test.enable = false' "${tmpdir}/local-git-hr.yaml" > "${tmpdir}/disabled-test-hr.yaml"
$runtime_resolver "${tmpdir}/disabled-test-hr.yaml" fallback > "${tmpdir}/disabled-test-runtime"
IFS=$'\t' read -r _ disabled_target disabled_tests < "${tmpdir}/disabled-test-runtime"
disabled_args=(template test-hook "$test_chart" -n "$disabled_target")
[[ $disabled_tests == true ]] || disabled_args+=(--skip-tests)
helm "${disabled_args[@]}" > "${tmpdir}/disabled-test-hook.yaml"
if grep -Fq 'docker.io/example/test:latest' "${tmpdir}/disabled-test-hook.yaml"; then
  echo 'FAIL disabled Helm test hook remained in effective workload output' >&2
  exit 1
fi

if $document_check "$fixtures/malformed-document.yaml" >/dev/null 2>&1; then
  echo 'FAIL malformed rendered Kubernetes document was accepted' >&2
  exit 1
fi

resolved_values="${tmpdir}/resolved-values.yaml"
$values_resolver --offline "$fixtures/helmrelease-values.yaml" values-test tools "$resolved_values"
assert_eq '2' "$(yq -r '.replicaCount' "$resolved_values")" 'inline values override valuesFrom'
assert_eq 'true' "$(yq -r '.nested.fromConfigMap' "$resolved_values")" 'ConfigMap valuesFrom merge'
assert_eq 'true' "$(yq -r '.nested.fromSecret' "$resolved_values")" 'Secret valuesFrom merge'
assert_eq 'target-secret' "$(yq -r '.config.token' "$resolved_values")" 'targetPath valuesFrom merge'
assert_eq 'true' "$(yq -r '.config."file.yaml".flags[0].enabled' "$resolved_values")" 'escaped and indexed targetPath merge'
assert_eq '!!bool' "$(yq -r '.config."file.yaml".flags[0].enabled | tag' "$resolved_values")" 'targetPath scalar typing'
assert_eq 'a,b=[c]' "$(yq -r '.config.literal' "$resolved_values")" 'literal targetPath merge'

mkdir -p "${tmpdir}/kubectl-notfound" "${tmpdir}/kubectl-transient"
cat > "${tmpdir}/kubectl-notfound/kubectl" <<'SH'
#!/usr/bin/env bash
printf 'Error from server (NotFound): configmaps "optional-missing" not found\n' >&2
exit 1
SH
cat > "${tmpdir}/kubectl-transient/kubectl" <<'SH'
#!/usr/bin/env bash
printf 'Unable to connect to the server\n' >&2
exit 1
SH
chmod +x "${tmpdir}/kubectl-notfound/kubectl" "${tmpdir}/kubectl-transient/kubectl"
PATH="${tmpdir}/kubectl-notfound:${PATH}" \
  $values_resolver "$fixtures/helmrelease-values.yaml" values-test tools "${tmpdir}/online-optional.yaml"
if PATH="${tmpdir}/kubectl-transient:${PATH}" \
  $values_resolver "$fixtures/helmrelease-values.yaml" values-test tools "${tmpdir}/online-transient.yaml" \
  >/dev/null 2>&1; then
  echo 'FAIL optional valuesFrom accepted a transient cluster read failure' >&2
  exit 1
fi

invalid_yaml="${tmpdir}/invalid.yaml"
printf 'apiVersion: [\n' > "$invalid_yaml"
if $document_check "$invalid_yaml" >/dev/null 2>&1; then
  echo 'FAIL malformed YAML was accepted as rendered Kubernetes output' >&2
  exit 1
fi
if $document_check "${tmpdir}/missing.yaml" >/dev/null 2>&1; then
  echo 'FAIL missing rendered manifest was accepted' >&2
  exit 1
fi

find_root="${tmpdir}/find-root"
mkdir -p "${find_root}/app" "${tmpdir}/bin"
cp "$fixtures/images-pinned.yaml" "${find_root}/app/workload.yaml"
cat > "${find_root}/app/kustomization.yaml" <<'YAML'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
- workload.yaml
YAML
real_find=$(command -v find)
cat > "${tmpdir}/bin/find" <<SH
#!/usr/bin/env bash
"${real_find}" "\$@"
exit 42
SH
chmod +x "${tmpdir}/bin/find"
if PATH="${tmpdir}/bin:${PATH}" $image_check "$find_root" >/dev/null 2>&1; then
  echo 'FAIL image-pin discovery accepted a producer failure' >&2
  exit 1
fi

printf 'ok: validation fixtures passed\n'
