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

gate=.github/workflows/image-gate.yaml
[[ ! -e .github/workflows/image-plan.yaml && ! -e .github/workflows/image-pull.yaml ]]
# Match literal workflow expressions, not shell expansions.
# shellcheck disable=SC2016
for required in \
  'pull_request_target:' \
  'ref: refs/heads/main' \
  'path: trusted' \
  'checks: write' \
  'persist-credentials: false' \
  'working_directory: trusted' \
  'install_args: python crane' \
  'gh api --paginate --slurp' \
  '--expected-count "$expected_file_count"' \
  'changed-files.json > changed-files.txt' \
  '[[ $(jq -r '\''.head.sha'\'' <<<"$pr_json") == "$HEAD_SHA" ]]' \
  'HEAD_REPOSITORY != "$REPOSITORY"' \
  'name: "Talos Image Availability"' \
  'Publish pending exact-head gate' \
  'Complete exact-head gate' \
  'HEAD_SHA: ${{ github.event.pull_request.head.sha }}' \
  'talos-image-availability:${RUN_ID}:${RUN_ATTEMPT}' \
  'trusted/scripts/talos-pr-files' \
  'trusted/scripts/talos-image-gate-scope' \
  'trusted/scripts/render-flate-image-inventory' \
  'trusted/scripts/talos-image-plan build' \
  'trusted/scripts/talos-image-plan verify' \
  'python3 trusted/scripts/check-registry-image-availability' \
  'GATE_RESULT: ${{ job.status }}' \
  "if: steps.scope.outputs.applicable == 'true'"; do
  grep -Fq -- "$required" "$gate"
done
grep -Fq -- '--restrict-egress' scripts/render-flate-image-inventory
if grep -Eq 'talos-image-pull|TALOSCONFIG|ghar-set-talos|talos-check-gate|workflow_run:|check-runs\?filter|sleep 15|Static Analysis|Flate - Success' "$gate"; then
  echo 'registry gate retains a cluster or other-workflow dependency' >&2
  exit 1
fi
if grep -Eq 'head/scripts/|base/scripts/|working_directory: (head|base)|cd (head|base)' "$gate"; then
  echo 'registry gate executes candidate code' >&2
  exit 1
fi
printf 'ok: registry workflow event and trust gates\n'
