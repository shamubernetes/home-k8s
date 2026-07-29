#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
tmp=$(mktemp -d -t hermes-firecrawl-promotion-test-XXXXXX)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/chart/firecrawl"

cat >"$tmp/bin/helm" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
[[ ${1:-} == pull ]]
destination=''
version=''
shift
while (( $# )); do
  case "$1" in
    --destination) destination=$2; shift 2 ;;
    --version) version=$2; shift 2 ;;
    *) shift ;;
  esac
done
mkdir -p "${PROMOTION_TEST_TMP:?}/chart/firecrawl"
printf 'apiVersion: v2\nname: firecrawl\nversion: %s\n' "$version" \
  >"${PROMOTION_TEST_TMP}/chart/firecrawl/Chart.yaml"
tar -czf "$destination/firecrawl-${version}.tgz" -C "${PROMOTION_TEST_TMP}/chart" firecrawl
printf 'Pulled: ghcr.io/shamubernetes/charts/firecrawl:%s\n' "$version"
printf 'Digest: %s\n' "${PROMOTION_TEST_ACTUAL_DIGEST:?}"
SH

cat >"$tmp/bin/cosign" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
[[ ${1:-} == verify ]]
printf '%s\n' "${*: -1}" >"${PROMOTION_TEST_TMP:?}/verified"
SH
chmod +x "$tmp/bin/helm" "$tmp/bin/cosign"

version=0.2.0-maude.2
digest=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
manifest="$tmp/ocirepository.yaml"
cat >"$manifest" <<'YAML'
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata:
  name: firecrawl
spec:
  ref:
    tag: 0.2.0-maude.1
YAML

PATH="$tmp/bin:$PATH" \
  PROMOTION_TEST_TMP="$tmp" \
  PROMOTION_TEST_ACTUAL_DIGEST="$digest" \
  FIRECRAWL_OCI_MANIFEST="$manifest" \
  "$root/scripts/promote-firecrawl-chart" "$version" "$digest" >/dev/null
grep -Fxq "    # firecrawl-oci-digest: $digest" "$manifest"
grep -Fxq "    tag: $version" "$manifest"
grep -Fxq "ghcr.io/shamubernetes/charts/firecrawl@$digest" "$tmp/verified"

bad=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
if PATH="$tmp/bin:$PATH" \
  PROMOTION_TEST_TMP="$tmp" \
  PROMOTION_TEST_ACTUAL_DIGEST="$bad" \
  FIRECRAWL_OCI_MANIFEST="$manifest" \
  "$root/scripts/promote-firecrawl-chart" "$version" "$digest" >/dev/null 2>&1; then
  printf 'promotion unexpectedly accepted the wrong digest\n' >&2
  exit 1
fi

printf 'Firecrawl promotion tests passed\n'
