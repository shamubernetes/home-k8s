#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

base_sha=1111111111111111111111111111111111111111
head_sha=2222222222222222222222222222222222222222
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
  --output "$tmpdir/plan.json"

actual=$(scripts/talos-image-plan verify \
  --plan "$tmpdir/plan.json" \
  --repository shamubernetes/home-k8s \
  --pull-request 42 \
  --base-sha "$base_sha" \
  --head-sha "$head_sha")
[[ $actual == "[\"${image}\"]" ]]

printf '%s\n' '["example/app:v2@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]' > "$tmpdir/head.json"
scripts/talos-image-plan build \
  --base-inventory "$tmpdir/base.json" \
  --head-inventory "$tmpdir/head.json" \
  --repository shamubernetes/home-k8s \
  --pull-request 42 \
  --base-sha "$base_sha" \
  --head-sha "$head_sha" \
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
  --output "$tmpdir/plan.json" >/dev/null 2>&1; then
  echo 'unapproved registry was accepted' >&2
  exit 1
fi

printf 'ok: Talos image plans bind trusted metadata to immutable approved images\n'
