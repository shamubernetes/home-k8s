#!/usr/bin/env bash
# Literal shell syntax is the injection payload under test.
# shellcheck disable=SC2016
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

event_file="${tmpdir}/event.json"
marker="${tmpdir}/payload-executed"
injection='$(touch '"$marker"') `touch '"$marker"'`'

jq -n \
  --arg message "render failed: ${injection}" \
  --arg reason "BuildFailed ${injection}" \
  '{
    client_payload: {
      involvedObject: {
        kind: "HelmRelease",
        name: "safe-release",
        namespace: "services"
      },
      message: $message,
      reason: $reason,
      timestamp: "2026-07-22T16:00:00Z",
      metadata: {
        "helm.toolkit.fluxcd.io/revision": "1.2.3"
      }
    }
  }' > "$event_file"

output=$(FLUX_FAILURE_RENDER_ONLY=true "${repo_root}/scripts/handle-flux-failure-event" "$event_file")
[[ ! -e $marker ]]
[[ $output == *'$('* ]]
[[ $output == *'safe-release'* ]]

jq '.client_payload.involvedObject.name = "bad;name"' "$event_file" > "${tmpdir}/invalid.json"
if FLUX_FAILURE_RENDER_ONLY=true "${repo_root}/scripts/handle-flux-failure-event" "${tmpdir}/invalid.json" >/dev/null 2>&1; then
  echo 'invalid Flux object name was accepted' >&2
  exit 1
fi

printf 'ok: Flux event payload is parsed as data and strict identifiers are enforced\n'
