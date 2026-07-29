#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

base_sha=1111111111111111111111111111111111111111
head_sha=2222222222222222222222222222222222222222
trigger_id='pull_request_target:42:synchronize:2026-07-29T00:33:35Z'
image='ghcr.io/example/app:v2@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
printf '%s\n' '["ghcr.io/example/app:v1@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"]' > "$tmpdir/base.json"
printf '["%s"]\n' "$image" > "$tmpdir/head.json"

scripts/talos-image-plan build \
  --base-inventory "$tmpdir/base.json" \
  --head-inventory "$tmpdir/head.json" \
  --repository shamubernetes/home-k8s \
  --pull-request 42 \
  --base-sha "$base_sha" \
  --head-sha "$head_sha" \
  --trigger-id "$trigger_id" \
  --output "$tmpdir/plan.json"

actual=$(scripts/talos-image-plan verify \
  --plan "$tmpdir/plan.json" \
  --repository shamubernetes/home-k8s \
  --pull-request 42 \
  --base-sha "$base_sha" \
  --head-sha "$head_sha" \
  --trigger-id "$trigger_id")
[[ $actual == "[\"${image}\"]" ]]

if scripts/talos-image-plan verify \
  --plan "$tmpdir/plan.json" \
  --repository shamubernetes/home-k8s \
  --pull-request 42 \
  --base-sha "$base_sha" \
  --head-sha "$head_sha" \
  --trigger-id 'pull_request_target:42:reopened:2026-07-29T00:34:00Z' \
  >/dev/null 2>&1; then
  echo 'mismatched trigger correlation was accepted' >&2
  exit 1
fi

printf '%s\n' '["example/app:v2@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]' > "$tmpdir/head.json"
scripts/talos-image-plan build \
  --base-inventory "$tmpdir/base.json" \
  --head-inventory "$tmpdir/head.json" \
  --repository shamubernetes/home-k8s \
  --pull-request 42 \
  --base-sha "$base_sha" \
  --head-sha "$head_sha" \
  --trigger-id "$trigger_id" \
  --output "$tmpdir/plan.json"
grep -Fq 'docker.io/example/app' "$tmpdir/plan.json"

printf '%s\n' '["ghcr.io/example/app:v2"]' > "$tmpdir/head.json"
if scripts/talos-image-plan build \
  --base-inventory "$tmpdir/base.json" \
  --head-inventory "$tmpdir/head.json" \
  --repository shamubernetes/home-k8s \
  --pull-request 42 \
  --base-sha "$base_sha" \
  --head-sha "$head_sha" \
  --trigger-id "$trigger_id" \
  --output "$tmpdir/plan.json" >/dev/null 2>&1; then
  echo 'mutable image was accepted' >&2
  exit 1
fi

printf '%s\n' '["evil.example/app:v2@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]' > "$tmpdir/head.json"
if scripts/talos-image-plan build \
  --base-inventory "$tmpdir/base.json" \
  --head-inventory "$tmpdir/head.json" \
  --repository shamubernetes/home-k8s \
  --pull-request 42 \
  --base-sha "$base_sha" \
  --head-sha "$head_sha" \
  --trigger-id "$trigger_id" \
  --output "$tmpdir/plan.json" >/dev/null 2>&1; then
  echo 'unapproved registry was accepted' >&2
  exit 1
fi

printf 'ok: Talos image plans bind trusted metadata to immutable approved images\n'

digest='sha256:ee6521f290b2168b6e0935a181d4cff9be1ac3f505666ef0e3c98fae8199917a'
read -r size unit < <(
  awk -v digest="$digest" '$3 == digest {print $4, $5; exit}' <<'TABLE'
NODE           IMAGE                                                                 DIGEST                                                                    SIZE     LABELS
10.100.47.50   registry.k8s.io/pause:3.10                                            sha256:ee6521f290b2168b6e0935a181d4cff9be1ac3f505666ef0e3c98fae8199917a   320 kB   io.cri-containerd.image=managed
10.100.47.50   registry.k8s.io/pause@sha256:ee6521f290b2168b6e0935a181d4cff9be1ac3f505666ef0e3c98fae8199917a   sha256:ee6521f290b2168b6e0935a181d4cff9be1ac3f505666ef0e3c98fae8199917a   320 kB   io.cri-containerd.image=managed
TABLE
)
[[ $size == 320 && $unit == kB ]]
printf 'ok: Talos image list size lookup\n'
