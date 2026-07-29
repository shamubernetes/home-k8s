#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

cat > "$tmpdir/success.json" <<'JSON'
{"check_runs":[
  {"id":11,"name":"Static Analysis - Success","status":"completed","conclusion":"success","app":{"id":15368}},
  {"id":12,"name":"Flate - Success","status":"completed","conclusion":"success","app":{"id":15368}}
]}
JSON
scripts/talos-check-gate \
  --checks "$tmpdir/success.json" \
  --app-id 15368 \
  --required 'Static Analysis - Success' \
  --required 'Flate - Success' >/dev/null

cat > "$tmpdir/spoof.json" <<'JSON'
{"check_runs":[
  {"id":21,"name":"Static Analysis - Success","status":"completed","conclusion":"success","app":{"id":99999}},
  {"id":22,"name":"Flate - Success","status":"completed","conclusion":"success","app":{"id":15368}}
]}
JSON
set +e
scripts/talos-check-gate \
  --checks "$tmpdir/spoof.json" \
  --app-id 15368 \
  --required 'Static Analysis - Success' \
  --required 'Flate - Success' >/dev/null 2>&1
status=$?
set -e
[[ $status -eq 10 ]]

cat > "$tmpdir/pending.json" <<'JSON'
{"check_runs":[
  {"id":31,"name":"Static Analysis - Success","status":"in_progress","conclusion":null,"app":{"id":15368}},
  {"id":32,"name":"Flate - Success","status":"completed","conclusion":"success","app":{"id":15368}}
]}
JSON
set +e
scripts/talos-check-gate \
  --checks "$tmpdir/pending.json" \
  --app-id 15368 \
  --required 'Static Analysis - Success' \
  --required 'Flate - Success' >/dev/null 2>&1
status=$?
set -e
[[ $status -eq 10 ]]

cat > "$tmpdir/failure.json" <<'JSON'
{"check_runs":[
  {"id":40,"name":"Static Analysis - Success","status":"completed","conclusion":"success","app":{"id":15368}},
  {"id":41,"name":"Static Analysis - Success","status":"completed","conclusion":"failure","app":{"id":15368}},
  {"id":42,"name":"Flate - Success","status":"completed","conclusion":"success","app":{"id":15368}}
]}
JSON
set +e
scripts/talos-check-gate \
  --checks "$tmpdir/failure.json" \
  --app-id 15368 \
  --required 'Static Analysis - Success' \
  --required 'Flate - Success' >/dev/null 2>&1
status=$?
set -e
if [[ $status -ne 1 ]]; then
  echo "latest trusted failed check returned unexpected status: $status" >&2
  exit 1
fi

workflow=.github/workflows/image-plan.yaml
grep -Fq "jq -r '.pull_request.number' \"\$GITHUB_EVENT_PATH\"" "$workflow"
if grep -Fq 'github.event_path' "$workflow"; then
  echo 'unsupported github.event_path context remains in planner' >&2
  exit 1
fi
grep -Fq 'github.event.pull_request.head.repo.full_name == github.repository' "$workflow"
grep -Fq 'Checkout trusted pull request base' "$workflow"
grep -Fq 'base/scripts/talos-image-plan build' "$workflow"
grep -Fq 'base/scripts/talos-image-plan verify' "$workflow"
if grep -Fq 'workflow_dispatch:' "$workflow" || grep -Fq 'shamubot[bot]' "$workflow"; then
  echo 'planner still contains a manual or author-specific execution path' >&2
  exit 1
fi

grep -Fq 'scripts/talos-check-gate' .github/workflows/image-pull.yaml
grep -Fq -- '--app-id 15368' .github/workflows/image-pull.yaml
grep -Fq "[[ \$SOURCE_EVENT == pull_request_target ]]" .github/workflows/image-pull.yaml
grep -Fq '(.previous_filename // empty)' .github/workflows/image-pull.yaml
grep -Fq -- '--paginate --slurp' .github/workflows/image-pull.yaml
grep -Fq 'mapfile -t changed_files < changed-files.txt' .github/workflows/image-pull.yaml
grep -Fq "(( \${#changed_files[@]} > 0 ))" .github/workflows/image-pull.yaml
if grep -Eq 'SOURCE_ACTOR|workflow_dispatch|shamubot\[bot\]' \
  .github/workflows/image-pull.yaml; then
  echo 'privileged consumer still depends on a manual or author-specific path' >&2
  exit 1
fi
grep -Fq 'name: "Talos Image Prepull"' .github/workflows/image-pull.yaml
grep -Fq 'checks: write' .github/workflows/image-pull.yaml
grep -Fq "always() && needs.preflight.outputs.head_sha != ''" .github/workflows/image-pull.yaml

gate=.github/workflows/image-gate.yaml
grep -Fq 'pull_request_target:' "$gate"
grep -Fq 'ref: refs/heads/main' "$gate"
grep -Fq 'name: Talos Image Availability' "$gate"
grep -Fq "HEAD_REPOSITORY != \"\$REPOSITORY\"" "$gate"
grep -Fq 'trusted/scripts/talos-image-gate-scope' "$gate"
grep -Fq '(.previous_filename // empty)' "$gate"
grep -Fq -- '--paginate --slurp' "$gate"
grep -Fq 'mapfile -t changed_files < changed-files.txt' "$gate"
grep -Fq "(( \${#changed_files[@]} > 0 ))" "$gate"
if grep -Eq 'trusted workflow dispatch|required for this same-repository|PULL_REQUEST_AUTHOR' "$gate"; then
  echo 'required gate still advertises manual planning' >&2
  exit 1
fi
grep -Fq -- "--required 'Talos Image Prepull'" "$gate"
grep -Fq -- "--required 'Render immutable image plan'" "$gate"
if grep -Fq 'actions/checkout' "$gate" && ! grep -Fq 'path: trusted' "$gate"; then
  echo 'Talos gate does not isolate its trusted checkout' >&2
  exit 1
fi

printf 'ok: Talos workflow event and trusted-check gates\n'
