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

printf 'ok: HelmRelease discovery is kind-based and skips are failure-classified\n'
