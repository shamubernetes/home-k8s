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
small="registry.k8s.io/small@$small_digest"
small2="registry.k8s.io/small2@$small2_digest"
small3="registry.k8s.io/small3@$small3_digest"
large="registry.k8s.io/large@$large_digest"

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
      */large@*) size=5000000001 ;;
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
    for state in "$FAKE_STATE/$node"-*; do
      [[ -e $state ]] || continue
      IFS=' ' read -r digest size unit < "$state"
      printf 'cri image %s %s %s\n' "$digest" "$size" "$unit"
    done
    ;;
  pull)
    image=$5
    digest=${image##*@}
    size=1
    unit=MB
    [[ $image != */oversize@* ]] || { size=6; unit=GB; }
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
    TALOS_NODES=node1,node2 IMAGES_JSON="[\"$image\"]" \
    PREFLIGHT_FLEET_BYTES="$bytes" scripts/talos-image-pull
}

run_pull "$tmpdir/success-state" "$tmpdir/success.log" "$small" >/dev/null
if grep -q 'image remove' "$tmpdir/success.log"; then
  echo 'successful pull unexpectedly invoked cleanup' >&2
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

: > "$tmpdir/preflight.log"
if run_pull "$tmpdir/preflight-state" "$tmpdir/preflight.log" "$small" 20000000001 >/dev/null 2>&1; then
  echo 'consumer accepted an oversized preventive size result' >&2
  exit 1
fi
[[ ! -s $tmpdir/preflight.log ]]

printf 'ok: preventive image limits and partial-pull cleanup fail closed\n'
