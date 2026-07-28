#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
fixture="${tmpdir}/repo"
mkdir -p \
  "${fixture}/scripts" \
  "${fixture}/kubernetes/apps/media/pasta/app" \
  "${fixture}/kubernetes/apps/media/pasta/app/chart/templates" \
  "${fixture}/kubernetes/flux/repositories/helm" \
  "${tmpdir}/bin"
cp "${repo_root}/scripts/check-oci-source-handoffs" "${fixture}/scripts/check-oci-source-handoffs"
chmod +x "${fixture}/scripts/check-oci-source-handoffs"

cat > "${fixture}/kubernetes/flux/repositories/helm/kustomization.yaml" <<'YAML'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
- bjw-s.yaml
YAML
cat > "${fixture}/kubernetes/flux/repositories/helm/bjw-s.yaml" <<'YAML'
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: bjw-s
  namespace: flux-system
spec:
  url: https://example.invalid/charts
YAML
cat > "${fixture}/kubernetes/apps/media/pasta/app/kustomization.yaml" <<'YAML'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
- helmrelease.yaml
YAML
cat > "${fixture}/kubernetes/apps/media/pasta/app/helmrelease.yaml" <<'YAML'
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: pasta
spec:
  interval: 10m
  chart:
    spec:
      chart: app-template
      version: 5.0.1
      sourceRef:
        kind: HelmRepository
        name: bjw-s
        namespace: flux-system
  install:
    remediation:
      retries: 3
YAML
cp "${fixture}/kubernetes/apps/media/pasta/app/helmrelease.yaml" "${tmpdir}/http-helmrelease.yaml"

cat > "${tmpdir}/bin/helm" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
[[ ${1:-} == pull ]]
chart=${2:-}
destination=''
while (( $# > 0 )); do
  if [[ $1 == --destination ]]; then
    destination=$2
    shift 2
    continue
  fi
  shift
done
[[ -n $destination ]]
mkdir -p "$destination"
content=same-package
if [[ ${FAKE_HELM_MISMATCH:-false} == true && $chart == oci://* ]]; then
  content=different-package
fi
printf '%s\n' "$content" > "${destination}/chart.tgz"
SH
chmod +x "${tmpdir}/bin/helm"

cat > "${fixture}/kubernetes/apps/media/pasta/app/chart/templates/configmap.yaml" <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}
data:
  value: {{ .Values.value }}
YAML

git -C "$fixture" init -q
git -C "$fixture" config user.email test@example.invalid
git -C "$fixture" config user.name test
git -C "$fixture" add .
git -C "$fixture" commit -qm 'base HTTP chart'
base=$(git -C "$fixture" rev-parse HEAD)

# A chart template is intentionally invalid as raw YAML. Changing it alongside
# a handoff must not make the guard feed Helm template syntax to yq.
cat > "${fixture}/kubernetes/apps/media/pasta/app/chart/templates/configmap.yaml" <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}
data:
  value: {{ default "updated" .Values.value }}
YAML

cat > "${fixture}/kubernetes/apps/media/pasta/app/ocirepository.yaml" <<'YAML'
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata:
  name: pasta
spec:
  interval: 30m
  layerSelector:
    mediaType: application/vnd.cncf.helm.chart.content.v1.tar+gzip
    operation: copy
  ref:
    tag: 5.0.1
  url: oci://ghcr.io/bjw-s-labs/helm/app-template
YAML
cat > "${fixture}/kubernetes/apps/media/pasta/app/kustomization.yaml" <<'YAML'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
- ocirepository.yaml
- helmrelease.yaml
YAML
cat > "${fixture}/kubernetes/apps/media/pasta/app/helmrelease.yaml" <<'YAML'
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: pasta
spec:
  interval: 10m
  chartRef:
    kind: OCIRepository
    name: pasta
  install:
    remediation:
      retries: 3
YAML
cp "${fixture}/kubernetes/apps/media/pasta/app/helmrelease.yaml" "${tmpdir}/oci-helmrelease.yaml"
cp "${fixture}/kubernetes/apps/media/pasta/app/ocirepository.yaml" "${tmpdir}/oci-source.yaml"

(
  cd "$fixture"
  PATH="${tmpdir}/bin:${PATH}" scripts/check-oci-source-handoffs "$base" >/dev/null
)

INTERVAL=30m yq '.spec.interval = strenv(INTERVAL)' \
  "${tmpdir}/oci-helmrelease.yaml" > "${fixture}/kubernetes/apps/media/pasta/app/helmrelease.yaml"
if (
  cd "$fixture"
  PATH="${tmpdir}/bin:${PATH}" scripts/check-oci-source-handoffs "$base" >/dev/null 2>&1
); then
  echo 'FAIL OCI handoff accepted a non-source HelmRelease change' >&2
  exit 1
fi
cp "${tmpdir}/oci-helmrelease.yaml" "${fixture}/kubernetes/apps/media/pasta/app/helmrelease.yaml"

TAG=5.0.2 yq '.spec.ref.tag = strenv(TAG)' \
  "${fixture}/kubernetes/apps/media/pasta/app/ocirepository.yaml" \
  > "${tmpdir}/bad-tag.yaml"
mv "${tmpdir}/bad-tag.yaml" "${fixture}/kubernetes/apps/media/pasta/app/ocirepository.yaml"
if (
  cd "$fixture"
  PATH="${tmpdir}/bin:${PATH}" scripts/check-oci-source-handoffs "$base" >/dev/null 2>&1
); then
  echo 'FAIL OCI handoff accepted a chart-version mismatch' >&2
  exit 1
fi
TAG=5.0.1 yq '.spec.ref.tag = strenv(TAG)' \
  "${fixture}/kubernetes/apps/media/pasta/app/ocirepository.yaml" \
  > "${tmpdir}/good-tag.yaml"
mv "${tmpdir}/good-tag.yaml" "${fixture}/kubernetes/apps/media/pasta/app/ocirepository.yaml"

if (
  cd "$fixture"
  FAKE_HELM_MISMATCH=true PATH="${tmpdir}/bin:${PATH}" \
    scripts/check-oci-source-handoffs "$base" >/dev/null 2>&1
); then
  echo 'FAIL OCI handoff accepted different HTTP and OCI chart packages' >&2
  exit 1
fi

ALLOW=true yq '.metadata.annotations."oci.home.arpa/allow-rollout" = strenv(ALLOW)' \
  "${fixture}/kubernetes/apps/media/pasta/app/ocirepository.yaml" \
  > "${tmpdir}/rollout-approved-source.yaml"
mv "${tmpdir}/rollout-approved-source.yaml" \
  "${fixture}/kubernetes/apps/media/pasta/app/ocirepository.yaml"
(
  cd "$fixture"
  FAKE_HELM_MISMATCH=true PATH="${tmpdir}/bin:${PATH}" \
    scripts/check-oci-source-handoffs "$base" >/dev/null
)
yq 'del(.metadata.annotations."oci.home.arpa/allow-rollout")' \
  "${fixture}/kubernetes/apps/media/pasta/app/ocirepository.yaml" \
  > "${tmpdir}/source-without-approval.yaml"
mv "${tmpdir}/source-without-approval.yaml" \
  "${fixture}/kubernetes/apps/media/pasta/app/ocirepository.yaml"

(
  cd "$fixture"
  git add .
  git commit -qm 'valid OCI handoff'
)
oci_base=$(git -C "$fixture" rev-parse HEAD)
cp "${tmpdir}/http-helmrelease.yaml" "${fixture}/kubernetes/apps/media/pasta/app/helmrelease.yaml"
(
  cd "$fixture"
  PATH="${tmpdir}/bin:${PATH}" scripts/check-oci-source-handoffs "$oci_base" >/dev/null
)

git -C "$fixture" reset --hard -q "$base"
URL=oci://ghcr.io/bjw-s-labs/helm yq '.spec.url = strenv(URL)' \
  "${fixture}/kubernetes/flux/repositories/helm/bjw-s.yaml" \
  > "${tmpdir}/oci-helmrepository.yaml"
mv "${tmpdir}/oci-helmrepository.yaml" \
  "${fixture}/kubernetes/flux/repositories/helm/bjw-s.yaml"
git -C "$fixture" add .
git -C "$fixture" commit -qm 'base OCI-backed HelmRepository'
oci_helmrepo_base=$(git -C "$fixture" rev-parse HEAD)
cp "${tmpdir}/oci-helmrelease.yaml" "${fixture}/kubernetes/apps/media/pasta/app/helmrelease.yaml"
cp "${tmpdir}/oci-source.yaml" "${fixture}/kubernetes/apps/media/pasta/app/ocirepository.yaml"
cat > "${fixture}/kubernetes/apps/media/pasta/app/kustomization.yaml" <<'YAML'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
- ocirepository.yaml
- helmrelease.yaml
YAML
if (
  cd "$fixture"
  PATH="${tmpdir}/bin:${PATH}" scripts/check-oci-source-handoffs "$oci_helmrepo_base" >/dev/null 2>&1
); then
  echo 'FAIL OCI-backed HelmRepository conversion did not require rollout approval' >&2
  exit 1
fi
ALLOW=true yq '.metadata.annotations."oci.home.arpa/allow-rollout" = strenv(ALLOW)' \
  "${fixture}/kubernetes/apps/media/pasta/app/ocirepository.yaml" \
  > "${tmpdir}/approved-oci-source.yaml"
mv "${tmpdir}/approved-oci-source.yaml" \
  "${fixture}/kubernetes/apps/media/pasta/app/ocirepository.yaml"
(
  cd "$fixture"
  PATH="${tmpdir}/bin:${PATH}" scripts/check-oci-source-handoffs "$oci_helmrepo_base" >/dev/null
)

printf 'ok: OCI source handoff fixtures passed\n'
