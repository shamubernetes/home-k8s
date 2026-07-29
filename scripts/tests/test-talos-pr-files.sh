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
scripts/talos-pr-files --expected-count 2 "$tmpdir/files.json" > "$tmpdir/paths"
grep -Fxq 'kubernetes/apps/services/vector/app/helmrelease.yaml' "$tmpdir/paths"
grep -Fxq 'docs/runner.md' "$tmpdir/paths"
grep -Fxq '.github/workflows/image-pull.yaml' "$tmpdir/paths"

printf '[]\n' > "$tmpdir/empty.json"
if scripts/talos-pr-files --expected-count 1 "$tmpdir/empty.json" >/dev/null 2>&1; then
  echo 'empty API response did not fail closed' >&2
  exit 1
fi

printf '[[{"status":"modified"}]]\n' > "$tmpdir/malformed.json"
if scripts/talos-pr-files --expected-count 1 "$tmpdir/malformed.json" >/dev/null 2>&1; then
  echo 'malformed API response did not fail closed' >&2
  exit 1
fi

if scripts/talos-pr-files --expected-count 3 "$tmpdir/files.json" >/dev/null 2>&1; then
  echo 'truncated API response did not fail closed' >&2
  exit 1
fi

if scripts/talos-pr-files --expected-count 3001 "$tmpdir/files.json" >/dev/null 2>&1; then
  echo 'GitHub 3,000-file API limit did not fail closed' >&2
  exit 1
fi

cat > "$tmpdir/duplicate.json" <<'JSON'
[[
  {"filename":"kubernetes/apps/services/vector/app/helmrelease.yaml","status":"modified"},
  {"filename":"kubernetes/apps/services/vector/app/helmrelease.yaml","status":"modified"}
]]
JSON
if scripts/talos-pr-files --expected-count 2 "$tmpdir/duplicate.json" >/dev/null 2>&1; then
  echo 'duplicate API filenames did not fail closed' >&2
  exit 1
fi

printf 'ok: PR file API parsing fails closed on truncation and preserves rename sources\n'
