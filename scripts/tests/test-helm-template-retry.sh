#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
mkdir -p "$tmpdir/bin"

cat > "$tmpdir/bin/helm" <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail
count=0
[[ ! -f $FAKE_HELM_COUNT ]] || count=$(< "$FAKE_HELM_COUNT")
count=$((count + 1))
printf '%s\n' "$count" > "$FAKE_HELM_COUNT"
printf 'attempt-%s\n' "$count"
if (( count < ${FAKE_HELM_SUCCEED_ON:-999} )); then
  exit "${FAKE_HELM_STATUS:-42}"
fi
SCRIPT
chmod +x "$tmpdir/bin/helm"

export PATH="$tmpdir/bin:$PATH"
export FAKE_HELM_COUNT="$tmpdir/count"
export HELM_TEMPLATE_RETRY_DELAY_SECONDS=0
export FAKE_HELM_SUCCEED_ON=3

scripts/helm-template-retry --output "$tmpdir/output.yaml" -- template test chart
[[ $(< "$tmpdir/count") == 3 ]]
[[ $(< "$tmpdir/output.yaml") == attempt-3 ]]

rm -f "$tmpdir/count"
printf 'stale\n' > "$tmpdir/failure.yaml"
export FAKE_HELM_SUCCEED_ON=999
set +e
scripts/helm-template-retry --output "$tmpdir/failure.yaml" -- template test chart >/dev/null 2>&1
status=$?
set -e
[[ $status == 42 ]]
[[ $(< "$tmpdir/count") == 3 ]]
[[ ! -e $tmpdir/failure.yaml ]]

if HELM_TEMPLATE_ATTEMPTS=0 scripts/helm-template-retry \
  --output "$tmpdir/invalid.yaml" -- template test chart >/dev/null 2>&1; then
  echo 'invalid retry count was accepted' >&2
  exit 1
fi

grep -Fq 'scripts/helm-template-retry' scripts/validate-app
grep -Fq 'scripts/helm-template-retry' scripts/tests/test-validation.sh
printf 'ok: Helm rendering retries producer failures and publishes atomically\n'
