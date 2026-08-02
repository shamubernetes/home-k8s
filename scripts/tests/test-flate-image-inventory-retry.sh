#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
mkdir -p "$tmpdir/bin" "$tmpdir/checkout/kubernetes"
printf '{}\n' > "$tmpdir/registry.json"

cat > "$tmpdir/bin/flate" <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail
count=0
[[ ! -f $FLATE_ATTEMPT_FILE ]] || count=$(<"$FLATE_ATTEMPT_FILE")
count=$((count + 1))
printf '%s\n' "$count" > "$FLATE_ATTEMPT_FILE"
if ((count < FLATE_SUCCEED_ON)); then
  exit "${FLATE_FAILURE_STATUS:-42}"
fi
printf '%s\n' "${FLATE_OUTPUT:-[]}"
SCRIPT
chmod +x "$tmpdir/bin/flate"

export PATH="$tmpdir/bin:$PATH"
export FLATE_ATTEMPT_FILE="$tmpdir/attempts"
export FLATE_IMAGE_INVENTORY_ATTEMPTS=3
export FLATE_IMAGE_INVENTORY_RETRY_DELAY_SECONDS=0
export FLATE_SUCCEED_ON=3
scripts/render-flate-image-inventory \
  --checkout "$tmpdir/checkout" \
  --registry-config "$tmpdir/registry.json" \
  --output "$tmpdir/images.json"
[[ $(<"$tmpdir/attempts") == 3 ]]
[[ $(jq -c . "$tmpdir/images.json") == '[]' ]]

rm -f "$tmpdir/attempts" "$tmpdir/images.json"
export FLATE_SUCCEED_ON=99
set +e
scripts/render-flate-image-inventory \
  --checkout "$tmpdir/checkout" \
  --registry-config "$tmpdir/registry.json" \
  --output "$tmpdir/images.json" >/dev/null 2>&1
status=$?
set -e
[[ $status == 42 ]]
[[ $(<"$tmpdir/attempts") == 3 ]]
[[ ! -e $tmpdir/images.json ]]

rm -f "$tmpdir/attempts"
export FLATE_SUCCEED_ON=1
export FLATE_OUTPUT='{}'
set +e
scripts/render-flate-image-inventory \
  --checkout "$tmpdir/checkout" \
  --registry-config "$tmpdir/registry.json" \
  --output "$tmpdir/images.json" >/dev/null 2>&1
status=$?
set -e
[[ $status == 1 ]]
[[ $(<"$tmpdir/attempts") == 1 ]]
[[ ! -e $tmpdir/images.json ]]

export FLATE_IMAGE_INVENTORY_ATTEMPTS=0
set +e
scripts/render-flate-image-inventory \
  --checkout "$tmpdir/checkout" \
  --registry-config "$tmpdir/registry.json" \
  --output "$tmpdir/images.json" >/dev/null 2>&1
status=$?
set -e
[[ $status == 2 ]]

grep -Fq 'trusted/scripts/render-flate-image-inventory' .github/workflows/image-plan.yaml
if grep -Fq 'flate --no-progress get images' .github/workflows/image-plan.yaml; then
  echo 'image-plan workflow still bypasses the retrying inventory wrapper' >&2
  exit 1
fi

printf 'ok: Flate image inventory retries producer failures and validates output\n'
