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
grep -Fq 'Checkout exact pull request base' "$workflow"
grep -Fq 'Checkout current trusted verifier' "$workflow"
grep -Fq 'ref: refs/heads/main' "$workflow"
grep -Fq 'path: trusted' "$workflow"
grep -Fq 'trusted/scripts/talos-image-plan build' "$workflow"
grep -Fq 'trusted/scripts/talos-image-plan verify' "$workflow"
grep -Fq 'name: Report immutable image plan' "$workflow"
grep -Fq 'name: "Talos Image Plan Attempt"' "$workflow"
grep -Fq "talos-image-plan:\${RUN_ID}:\${trigger_id}" "$workflow"
grep -Fq 'checks: write' "$workflow"
if grep -Fq 'workflow_dispatch:' "$workflow" || grep -Fq 'shamubot[bot]' "$workflow"; then
  echo 'planner still contains a manual or author-specific execution path' >&2
  exit 1
fi

grep -Fq 'scripts/talos-check-gate' .github/workflows/image-pull.yaml
grep -Fq -- '--app-id 15368' .github/workflows/image-pull.yaml
grep -Fq "[[ \$SOURCE_EVENT == pull_request_target ]]" .github/workflows/image-pull.yaml
grep -Fq -- '--paginate --slurp' .github/workflows/image-pull.yaml
grep -Fq -- "--expected-count \"\$expected_file_count\"" .github/workflows/image-pull.yaml
grep -Fq 'changed-files.json > changed-files.txt' .github/workflows/image-pull.yaml
grep -Fq "'.changed_files | select(type == \"number\" and . >= 1 and floor == .)'" \
  .github/workflows/image-pull.yaml
grep -Fq "scope=\$(scripts/talos-image-gate-scope < changed-files.txt)" \
  .github/workflows/image-pull.yaml
grep -Fq "[[ \$scope == applicable ]]" .github/workflows/image-pull.yaml
grep -Fq "if [[ \$scope == not-applicable ]]" .github/workflows/image-pull.yaml
grep -Fq "if: \${{ needs.preflight.outputs.applicable == 'true' }}" .github/workflows/image-pull.yaml
grep -Fq 'scripts/talos-image-size' .github/workflows/image-pull.yaml
grep -Fq 'PREFLIGHT_FLEET_BYTES' .github/workflows/image-pull.yaml
grep -Fq 'scripts/talos-image-pull' .github/workflows/image-pull.yaml
grep -Fq "ref: \${{ needs.preflight.outputs.trusted_sha }}" .github/workflows/image-pull.yaml
grep -Fq 'environment: talos-image-pull' .github/workflows/image-pull.yaml
if grep -Eq 'environment_url|workflow_dispatch|type: approval' .github/workflows/image-pull.yaml; then
  echo 'privileged consumer contains a manual execution or approval gate' >&2
  exit 1
fi
if grep -Fq "protected='^(" .github/workflows/image-pull.yaml; then
  echo 'consumer duplicates the shared privileged-boundary classifier' >&2
  exit 1
fi
grep -Fq 'SOURCE_HEAD_SHA' .github/workflows/image-pull.yaml
grep -Fq "[[ \$(jq -r '.base.ref' <<<\"\$pr_json\") == main ]]" \
  .github/workflows/image-pull.yaml
grep -Fq "compare/\${base_sha}...\${current_base_sha}" .github/workflows/image-pull.yaml
grep -Fq 'identical|ahead)' .github/workflows/image-pull.yaml
if grep -Fq "'.base.sha' <<<\"\$pr_json\") == \"\$base_sha\"" \
  .github/workflows/image-pull.yaml; then
  echo 'consumer still requires a live base SHA equality that races default-branch updates' >&2
  exit 1
fi
if grep -Eq 'SOURCE_ACTOR|workflow_dispatch|shamubot\[bot\]' \
  .github/workflows/image-pull.yaml; then
  echo 'privileged consumer still depends on a manual or author-specific path' >&2
  exit 1
fi
grep -Fq 'name: "Talos Image Prepull"' .github/workflows/image-pull.yaml
grep -Fq 'checks: write' .github/workflows/image-pull.yaml
grep -Fq "always() && needs.preflight.outputs.head_sha != ''" .github/workflows/image-pull.yaml
grep -Fq 'gh api --paginate --slurp' .github/workflows/image-pull.yaml
grep -Fq "external_id: \$external_id" .github/workflows/image-pull.yaml
grep -Fq "talos-image-prepull:\${SOURCE_RUN_ID}:\${GITHUB_RUN_ID}" \
  .github/workflows/image-pull.yaml
grep -Fq ":\${TRIGGER_ID}" .github/workflows/image-pull.yaml
grep -Fq "name: talos-image-prepull-result-\${{ github.run_id }}" \
  .github/workflows/image-pull.yaml
grep -Fq 'talos-image-prepull-result.json' .github/workflows/image-pull.yaml
grep -Fq "consumer_run_id: \$consumer_run_id" .github/workflows/image-pull.yaml
if grep -Fq 'group: talos-image-availability' .github/workflows/image-pull.yaml; then
  echo 'Talos image pulls remain globally serialized ahead of the one-runner queue' >&2
  exit 1
fi

gate=.github/workflows/image-gate.yaml
grep -Fq 'pull_request_target:' "$gate"
grep -Fq 'ref: refs/heads/main' "$gate"
grep -Fq 'name: "Talos Image Availability"' "$gate"
grep -Fq 'name: Evaluate Talos image availability' "$gate"
grep -Fq 'checks: write' "$gate"
grep -Fq 'Publish pending exact-head gate' "$gate"
grep -Fq 'Complete exact-head gate' "$gate"
grep -Fq "talos-image-availability:\${RUN_ID}:\${RUN_ATTEMPT}" "$gate"
grep -Fq "HEAD_REPOSITORY != \"\$REPOSITORY\"" "$gate"
grep -Fq 'gh api --paginate --slurp' "$gate"
grep -Fq "pr_json=\$(gh api \"repos/\${REPOSITORY}/pulls/\${PULL_REQUEST}\")" "$gate"
grep -Fq -- "--expected-count \"\$expected_file_count\"" "$gate"
grep -Fq 'changed-files.json > changed-files.txt' "$gate"
grep -Fq 'trusted/scripts/talos-image-gate-scope' "$gate"
grep -Fq -- '--paginate --slurp' "$gate"
if grep -Eq 'trusted workflow dispatch|required for this same-repository|PULL_REQUEST_AUTHOR' "$gate"; then
  echo 'required gate still advertises manual planning' >&2
  exit 1
fi
grep -Fq -- "--required 'Talos Image Prepull'" "$gate"
grep -Fq -- "--required 'Talos Image Plan Attempt'" "$gate"
grep -Fq "trigger_id == \"\$expected_trigger\"" "$gate"
grep -Fq '.github/workflows/image-plan.yaml' "$gate"
grep -Fq '.github/workflows/image-pull.yaml' "$gate"
grep -Fq 'talos-image-plan:' "$gate"
grep -Fq 'check-runs?filter=all&per_page=100' "$gate"
grep -Fq 'actions: read' "$gate"
grep -Fq "if [[ \$check_conclusion != success ]]; then" "$gate"
grep -Fq 'download_exact_artifact' "$gate"
grep -Fq 'trusted/scripts/talos-image-plan verify' "$gate"
grep -Fq "talos-image-prepull-result-\${consumer_run_id}" "$gate"
grep -Fq ".consumer_run_id == \$consumer_run_id" "$gate"
if grep -Fq "'.html_url'" "$gate"; then
  echo 'gate still trusts GitHub-rewritten custom-check details URLs' >&2
  exit 1
fi
grep -Fq 'startsWith("talos-image-prepull:")' "$gate" || \
  grep -Fq 'startswith("talos-image-prepull:")' "$gate"
if grep -Fq 'actions/checkout' "$gate" && ! grep -Fq 'path: trusted' "$gate"; then
  echo 'Talos gate does not isolate its trusted checkout' >&2
  exit 1
fi

grep -Fq "pull_request_target:\${pr}:\${EVENT_ACTION}:\${EVENT_UPDATED_AT}" \
  .github/workflows/image-plan.yaml
grep -Fq -- "--trigger-id \"\$TRIGGER_ID\"" .github/workflows/image-plan.yaml

printf 'ok: Talos workflow event and trusted-check gates\n'
