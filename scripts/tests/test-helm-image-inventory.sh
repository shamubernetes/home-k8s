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

mkdir -p "${repo}/kubernetes/apps/test/malformed/app"
cat > "${repo}/kubernetes/apps/test/malformed/app/helmrelease.yaml" <<'YAML'
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata: [
YAML
git -C "$repo" add kubernetes/apps/test/malformed/app/helmrelease.yaml
if (
  cd "$repo"
  VALIDATE_CALLS="$calls" VALIDATE_MODE=success scripts/check-helmrelease-images >/dev/null 2>&1
); then
  echo 'malformed HelmRelease candidate YAML was accepted' >&2
  exit 1
fi
git -C "$repo" rm -q -f kubernetes/apps/test/malformed/app/helmrelease.yaml

mkdir -p "${repo}/kubernetes/apps/test/second/app" "${tmpdir}/sync"
cp "${repo}/kubernetes/apps/test/alt/app/custom-release.yaml" \
  "${repo}/kubernetes/apps/test/second/app/custom-release.yaml"
git -C "$repo" add kubernetes/apps/test/second/app/custom-release.yaml
(
  cd "$repo"
  HELM_IMAGE_JOBS=2 VALIDATE_CALLS="$calls" VALIDATE_MODE=concurrent \
    VALIDATE_SYNC="${tmpdir}/sync" scripts/check-helmrelease-images >/dev/null
)

matrix=$("${repo_root}/scripts/plan-helm-image-shards" 101)
if ! jq -e '
  .include | length == 6
  and map(.size) == [17, 17, 17, 17, 17, 16]
  and map(.count) == [6, 6, 6, 6, 6, 6]
  and map(.index) == [0, 1, 2, 3, 4, 5]
' <<< "$matrix" >/dev/null; then
  echo '101 applications were not balanced across six shards' >&2
  exit 1
fi

discovered_matrix=$(
  cd "$repo"
  "${repo_root}/scripts/plan-helm-image-shards"
)
if ! jq -e '.include | length == 1 and .[0].size == 2' \
  <<< "$discovered_matrix" >/dev/null; then
  echo 'discovery mode did not count the two fixture applications' >&2
  exit 1
fi

real_git=$(command -v git)
mkdir -p "${tmpdir}/shim"
cat > "${tmpdir}/shim/git" <<'SH'
#!/usr/bin/env bash
if [[ ${1:-} == grep ]]; then
  echo 'kubernetes/apps/test/alt/app/custom-release.yaml'
  exit 2
fi
exec "$REAL_GIT" "$@"
SH
chmod +x "${tmpdir}/shim/git"
if (
  cd "$repo"
  REAL_GIT="$real_git" PATH="${tmpdir}/shim:${PATH}" \
    "${repo_root}/scripts/plan-helm-image-shards" >/dev/null 2>&1
); then
  echo 'shard discovery accepted partial output from a failed git producer' >&2
  exit 1
fi

if (
  cd "$repo"
  HELM_IMAGE_SHARD_COUNT=0 HELM_IMAGE_SHARD_INDEX=0 \
    VALIDATE_CALLS="$calls" VALIDATE_MODE=success scripts/check-helmrelease-images >/dev/null 2>&1
); then
  echo 'zero shard count was accepted' >&2
  exit 1
fi
if (
  cd "$repo"
  HELM_IMAGE_SHARD_COUNT=2 HELM_IMAGE_SHARD_INDEX=2 \
    VALIDATE_CALLS="$calls" VALIDATE_MODE=success scripts/check-helmrelease-images >/dev/null 2>&1
); then
  echo 'out-of-range shard index was accepted' >&2
  exit 1
fi

real_yq=$(command -v yq)
yq_calls="${tmpdir}/yq-calls"
yq_invocations="${tmpdir}/yq-invocations"
yq_shim="${tmpdir}/yq-shim"
mkdir -p "$yq_shim"
: > "$yq_calls"
: > "$yq_invocations"
cat > "${yq_shim}/yq" <<'SH'
#!/usr/bin/env bash
printf 'invoked\n' >> "$YQ_INVOCATIONS"
for arg in "$@"; do
  case "$arg" in
    */custom-release.yaml)
      printf '%s\n' "$arg" >> "$YQ_CALLS"
      ;;
  esac
done
exec "$REAL_YQ" "$@"
SH
chmod +x "${yq_shim}/yq"

: > "$calls"
for shard in 0 1; do
  (
    cd "$repo"
    REAL_YQ="$real_yq" YQ_CALLS="$yq_calls" YQ_INVOCATIONS="$yq_invocations" \
      PATH="${yq_shim}:${PATH}" \
      HELM_IMAGE_SHARD_COUNT=2 HELM_IMAGE_SHARD_INDEX=$shard \
      VALIDATE_CALLS="$calls" VALIDATE_MODE=success scripts/check-helmrelease-images >/dev/null
  )
done
sort -u "$calls" > "${tmpdir}/sharded-calls"
printf '%s\n' test/alt/app test/second/app > "${tmpdir}/expected-sharded-calls"
diff -u "${tmpdir}/expected-sharded-calls" "${tmpdir}/sharded-calls"
if [[ $(awk 'END { print NR }' "$yq_invocations") != 2 ]]; then
  echo 'each shard did not batch-parse HelmRelease candidates exactly once' >&2
  exit 1
fi
if ! awk '
  { count[$0]++ }
  END {
    if (length(count) != 2) exit 1
    for (file in count) if (count[file] != 2) exit 1
  }
' "$yq_calls"; then
  echo 'each batch parse did not cover every HelmRelease candidate' >&2
  exit 1
fi

printf 'ok: HelmRelease discovery, classified skips, concurrency, and balanced dynamic sharding validated\n'
