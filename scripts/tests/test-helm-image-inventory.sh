#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
repo="${tmpdir}/repo"
mkdir -p "${repo}/scripts" "${repo}/kubernetes/apps/test/alt/app"
cp "${repo_root}/scripts/check-helmrelease-images" "${repo}/scripts/check-helmrelease-images"

cat > "${repo}/kubernetes/apps/test/alt/app/custom-release.yaml" <<'YAML'
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: alternate-filename
YAML
cat > "${repo}/scripts/helm-image-pin-baseline.txt" <<'TXT'
# app-directory<TAB>rendered-image
TXT
cat > "${repo}/scripts/helm-image-validation-skips.txt" <<'TXT'
# app-directory<TAB>expected diagnostic substring<TAB>reason
test/alt/app	expected external value is unavailable	test-only expected render gap
TXT
cat > "${repo}/scripts/validate-app" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "${2:-}" >> "${VALIDATE_CALLS}"
case "${VALIDATE_MODE}" in
  expected)
    echo 'expected external value is unavailable' >&2
    exit 1
    ;;
  unrelated)
    echo 'missing required command: helm' >&2
    exit 1
    ;;
  mixed)
    echo '==> kustomize kubernetes/apps/test/alt/app' >&2
    echo 'expected external value is unavailable' >&2
    echo 'missing required command: helm' >&2
    exit 1
    ;;
  success)
    exit 0
    ;;
  concurrent)
    marker=${2//\//_}
    touch "${VALIDATE_SYNC}/${marker}"
    for _ in {1..100}; do
      markers=("${VALIDATE_SYNC}"/*)
      (( ${#markers[@]} >= 2 )) && exit 0
      sleep 0.02
    done
    echo 'validators did not overlap' >&2
    exit 1
    ;;
  *)
    exit 2
    ;;
esac
SH
chmod +x "${repo}/scripts/check-helmrelease-images" "${repo}/scripts/validate-app"

git -C "$repo" init -q
git -C "$repo" config user.email test@example.invalid
git -C "$repo" config user.name test
git -C "$repo" add .
git -C "$repo" commit -qm fixture

calls="${tmpdir}/calls"
: > "$calls"
(
  cd "$repo"
  VALIDATE_CALLS="$calls" VALIDATE_MODE=expected scripts/check-helmrelease-images >/dev/null
)
if ! grep -Fxq 'test/alt/app' "$calls"; then
  echo 'HelmRelease under an alternate filename was not discovered' >&2
  exit 1
fi

: > "$calls"
if (
  cd "$repo"
  VALIDATE_CALLS="$calls" VALIDATE_MODE=unrelated scripts/check-helmrelease-images >/dev/null 2>&1
); then
  echo 'classified Helm render skip accepted an unrelated failure' >&2
  exit 1
fi
if (
  cd "$repo"
  VALIDATE_CALLS="$calls" VALIDATE_MODE=mixed scripts/check-helmrelease-images >/dev/null 2>&1
); then
  echo 'classified Helm render skip hid an additional failure' >&2
  exit 1
fi

cat > "${repo}/scripts/helm-image-validation-skips.txt" <<'TXT'
# app-directory<TAB>expected diagnostic substring<TAB>reason
TXT
: > "$calls"
(
  cd "$repo"
  VALIDATE_CALLS="$calls" VALIDATE_MODE=success scripts/check-helmrelease-images >/dev/null
)
if ! grep -Fxq 'test/alt/app' "$calls"; then
  echo 'alternate-filename HelmRelease was not enforced after removing the skip' >&2
  exit 1
fi

mkdir -p "${repo}/kubernetes/apps/test/second/app" "${tmpdir}/sync"
cp "${repo}/kubernetes/apps/test/alt/app/custom-release.yaml" \
  "${repo}/kubernetes/apps/test/second/app/custom-release.yaml"
git -C "$repo" add kubernetes/apps/test/second/app/custom-release.yaml
(
  cd "$repo"
  HELM_IMAGE_JOBS=2 VALIDATE_CALLS="$calls" VALIDATE_MODE=concurrent \
    VALIDATE_SYNC="${tmpdir}/sync" scripts/check-helmrelease-images >/dev/null
)

: > "$calls"
for shard in 0 1; do
  (
    cd "$repo"
    HELM_IMAGE_SHARD_COUNT=2 HELM_IMAGE_SHARD_INDEX=$shard \
      VALIDATE_CALLS="$calls" VALIDATE_MODE=success scripts/check-helmrelease-images >/dev/null
  )
done
sort -u "$calls" > "${tmpdir}/sharded-calls"
printf '%s\n' test/alt/app test/second/app > "${tmpdir}/expected-sharded-calls"
diff -u "${tmpdir}/expected-sharded-calls" "${tmpdir}/sharded-calls"

printf 'ok: HelmRelease discovery, classified skips, concurrency, and sharding validated\n'
