#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

cat > "$tmpdir/files.json" <<'JSON'
[[
  {"filename":"kubernetes/apps/services/vector/app/helmrelease.yaml","status":"modified"},
  {"filename":"docs/runner.md","previous_filename":".github/workflows/image-pull.yaml","status":"renamed"}
]]
JSON
scripts/talos-pr-files "$tmpdir/files.json" > "$tmpdir/paths"
grep -Fxq 'kubernetes/apps/services/vector/app/helmrelease.yaml' "$tmpdir/paths"
grep -Fxq 'docs/runner.md' "$tmpdir/paths"
grep -Fxq '.github/workflows/image-pull.yaml' "$tmpdir/paths"

printf '[]\n' > "$tmpdir/empty.json"
if scripts/talos-pr-files "$tmpdir/empty.json" >/dev/null 2>&1; then
  echo 'empty API response did not fail closed' >&2
  exit 1
fi

printf '[[{"status":"modified"}]]\n' > "$tmpdir/malformed.json"
if scripts/talos-pr-files "$tmpdir/malformed.json" >/dev/null 2>&1; then
  echo 'malformed API response did not fail closed' >&2
  exit 1
fi

printf 'ok: PR file API parsing fails closed and preserves rename sources\n'
