#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

small_digest=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
small2_digest=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
small3_digest=sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
large_digest=sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
partial_digest=sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
small="registry.k8s.io/small@$small_digest"
small2="registry.k8s.io/small2@$small2_digest"
small3="registry.k8s.io/small3@$small3_digest"
large="registry.k8s.io/large@$large_digest"
partial="registry.k8s.io/partial@$partial_digest"

cat > "$tmpdir/crane" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
command=$1
image=$2
case "$command" in
  digest)
    printf '%s\n' "${image##*@}"
    ;;
  config)
    if [[ $image == */wrong-platform@* ]]; then
      printf '{"os":"linux","architecture":"arm64"}\n'
    else
      printf '{"os":"linux","architecture":"amd64"}\n'
    fi
    ;;
  manifest)
    case "$image" in
      */large@*) size=6000000001 ;;
      */small2@*|*/small3@*) size=4000000000 ;;
      *) size=300 ;;
    esac
    printf '{"mediaType":"application/vnd.oci.image.manifest.v1+json","config":{"size":0},"layers":[{"size":%s}]}\n' "$size"
    ;;
  *) exit 2 ;;
esac
SH
chmod +x "$tmpdir/crane"

CRANE="$tmpdir/crane" scripts/talos-image-size \
  --images-json "[\"$small\"]" --node-count 2 --output "$tmpdir/size.json"
[[ $(jq -r '.fleet_total_bytes' "$tmpdir/size.json") -eq 600 ]]
CRANE="$tmpdir/crane" scripts/talos-image-size \
  --images-json '[]' --node-count 2 --output "$tmpdir/empty-size.json"
[[ $(jq -r '.per_node_total_bytes' "$tmpdir/empty-size.json") -eq 0 ]]
[[ $(jq -r '.fleet_total_bytes' "$tmpdir/empty-size.json") -eq 0 ]]
[[ $(jq -r '.images | length' "$tmpdir/empty-size.json") -eq 0 ]]
if CRANE="$tmpdir/crane" scripts/talos-image-size \
    --images-json "[\"$large\"]" --node-count 2 >/dev/null 2>&1; then
  echo 'preventive per-image limit accepted an oversized manifest' >&2
  exit 1
fi
wrong_platform="registry.k8s.io/wrong-platform@$small3_digest"
if CRANE="$tmpdir/crane" scripts/talos-image-size \
    --images-json "[\"$wrong_platform\"]" --node-count 2 >/dev/null 2>&1; then
  echo 'preventive verifier accepted the wrong image platform' >&2
  exit 1
fi
if CRANE="$tmpdir/crane" scripts/talos-image-size \
    --images-json "[\"$small2\",\"$small3\",\"$small\"]" --node-count 3 >/dev/null 2>&1; then
  echo 'preventive fleet limit accepted an oversized plan' >&2
  exit 1
fi

cat > "$tmpdir/talosctl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_LOG"
if [[ $1 == version ]]; then
  echo 'Client: v1.13.7'
  exit 0
fi
[[ $1 == --nodes ]]
node=$2
[[ $3 == image ]]
command=$4
state_name() {
  printf '%s/%s-%s' "$FAKE_STATE" "$node" "${1//:/_}"
}
case "$command" in
  list)
    if [[ ${FAKE_LIST_FAILURE:-} == 1 && ! -e $FAKE_STATE/list-failed ]]; then
      touch "$FAKE_STATE/list-failed"
      exit 17
    fi
    for state in "$FAKE_STATE/$node"-*; do
      [[ -e $state ]] || continue
      IFS=' ' read -r digest size unit < "$state"
      printf 'cri image %s %s %s\n' "$digest" "$size" "$unit"
    done
    if [[ ${FAKE_LIST_TRAILING:-} == 1 ]]; then
      for _ in $(seq 1 10000); do
        printf 'cri image sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff 1 MB\n'
      done
    fi
    ;;
  pull)
    image=$5
    digest=${image##*@}
    [[ $image != */partial@* || $node != node2 ]] || exit 43
    size=1
    unit=MB
    [[ $image != */oversize@* ]] || { size=7; unit=GB; }
    printf '%s %s %s\n' "$digest" "$size" "$unit" > "$(state_name "$digest")"
    [[ $image != */fail@* || $node != node2 ]] || exit 42
    ;;
  remove)
    image=$5
    digest=${image##*@}
    rm -f "$(state_name "$digest")"
    ;;
  *) exit 2 ;;
esac
SH
chmod +x "$tmpdir/talosctl"
cat > "$tmpdir/timeout" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
shift
exec "$@"
SH
chmod +x "$tmpdir/timeout"

run_pull() {
  local state=$1 log=$2 image=$3 bytes=${4:-1000}
  mkdir -p "$state"
  PATH="$tmpdir:$PATH" FAKE_STATE="$state" FAKE_LOG="$log" TALOSCTL="$tmpdir/talosctl" \
    FAKE_LIST_FAILURE="${FAKE_LIST_FAILURE:-}" FAKE_LIST_TRAILING="${FAKE_LIST_TRAILING:-}" \
    TALOS_NODES=node1,node2 IMAGES_JSON="[\"$image\"]" \
    PREFLIGHT_FLEET_BYTES="$bytes" scripts/talos-image-pull
}

run_pull "$tmpdir/success-state" "$tmpdir/success.log" "$small" >/dev/null
if grep -q 'image remove' "$tmpdir/success.log"; then
  echo 'successful pull unexpectedly invoked cleanup' >&2
  exit 1
fi

mkdir -p "$tmpdir/empty-state"
PATH="$tmpdir:$PATH" FAKE_STATE="$tmpdir/empty-state" FAKE_LOG="$tmpdir/empty.log" \
  TALOSCTL="$tmpdir/talosctl" TALOS_NODES=node1,node2 IMAGES_JSON='[]' \
  PREFLIGHT_FLEET_BYTES=0 scripts/talos-image-pull > "$tmpdir/empty.out"
grep -q '^completed total_bytes=0$' "$tmpdir/empty.out"
if grep -Eq 'image (list|pull|remove)' "$tmpdir/empty.log"; then
  echo 'empty plan invoked a Talos image operation' >&2
  exit 1
fi

list_failure_state="$tmpdir/list-failure-state"
mkdir -p "$list_failure_state"
printf '%s %s %s\n' "$small_digest" 1 MB > "$list_failure_state/node1-${small_digest//:/_}"
if FAKE_LIST_FAILURE=1 run_pull "$list_failure_state" "$tmpdir/list-failure.log" "$small" >/dev/null 2>&1; then
  echo 'pre-pull inventory failure unexpectedly succeeded' >&2
  exit 1
fi
grep -q -- '--nodes node1 image list' "$tmpdir/list-failure.log"
if grep -Eq 'image (pull|remove)' "$tmpdir/list-failure.log"; then
  echo 'pre-pull inventory failure incorrectly claimed or removed an image' >&2
  exit 1
fi
[[ -e $list_failure_state/node1-${small_digest//:/_} ]]

trailing_state="$tmpdir/trailing-state"
mkdir -p "$trailing_state"
printf '%s %s %s\n' "$small_digest" 1 MB > "$trailing_state/node1-${small_digest//:/_}"
FAKE_LIST_TRAILING=1 run_pull "$trailing_state" "$tmpdir/trailing.log" "$small" >/dev/null
if grep -q 'image remove' "$tmpdir/trailing.log"; then
  echo 'complete inventory output caused a false cleanup claim' >&2
  exit 1
fi

oversize="registry.k8s.io/oversize@$large_digest"
if run_pull "$tmpdir/oversize-state" "$tmpdir/oversize.log" "$oversize" >/dev/null 2>&1; then
  echo 'post-pull size verifier accepted an oversized image' >&2
  exit 1
fi
grep -q 'image remove' "$tmpdir/oversize.log"
[[ -z $(find "$tmpdir/oversize-state" -type f -print -quit) ]]

failed="registry.k8s.io/fail@$small2_digest"
if run_pull "$tmpdir/fail-state" "$tmpdir/fail.log" "$failed" >/dev/null 2>&1; then
  echo 'failed Talos pull unexpectedly succeeded' >&2
  exit 1
fi
[[ $(grep -c 'image remove' "$tmpdir/fail.log") -eq 2 ]]
[[ -z $(find "$tmpdir/fail-state" -type f -print -quit) ]]

if run_pull "$tmpdir/partial-state" "$tmpdir/partial.log" "$partial" >/dev/null 2>&1; then
  echo 'genuinely partial Talos pull unexpectedly succeeded' >&2
  exit 1
fi
[[ $(grep -c 'image remove' "$tmpdir/partial.log") -eq 2 ]]
grep -q -- '--nodes node2 image pull' "$tmpdir/partial.log"
grep -q -- '--nodes node2 image remove' "$tmpdir/partial.log"
[[ -z $(find "$tmpdir/partial-state" -type f -print -quit) ]]

# Completed and run-owned registrations are removed immediately. If a pull
# fails before registration, CRI cancellation plus RemoveImage leaves the
# partial content unreferenced for containerd GC; kubelet also keeps image
# pressure bounded on these worker nodes.
yq -e '.machine.kubelet.extraConfig.imageGCHighThresholdPercent == 55 and
  .machine.kubelet.extraConfig.imageGCLowThresholdPercent == 50 and
  .machine.kubelet.extraConfig.imageMinimumGCAge == "2m"' \
  talos/patches/global/kubelet.yaml >/dev/null

: > "$tmpdir/preflight.log"
if run_pull "$tmpdir/preflight-state" "$tmpdir/preflight.log" "$small" 20000000001 >/dev/null 2>&1; then
  echo 'consumer accepted an oversized preventive size result' >&2
  exit 1
fi
[[ ! -s $tmpdir/preflight.log ]]

printf 'ok: preventive limits, no-op plans, inventory ownership, and partial-pull cleanup fail closed\n'
