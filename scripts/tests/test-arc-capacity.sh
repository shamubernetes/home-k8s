#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

# These are owner-required capacity floors, not incident-tuning defaults.
check_capacity() {
  yq -e '
    (.spec.maxRunners | tag) == "!!int" and
    .spec.maxRunners >= 8 and
    .spec.minRunners == 0
  ' "$1" >/dev/null
}

for set in zoo maudecode; do
  app="kubernetes/apps/actions-runner-system/ghar-scale-set/arc-${set}"
  yq '.spec.values' "$app/helmrelease.yaml" > "$tmpdir/values.yaml"
  chart_url=$(yq -r '.spec.url' "$app/ocirepository.yaml")
  chart_version=$(yq -r '.spec.ref.tag' "$app/ocirepository.yaml")
  helm template "ghar-set-${set}" "$chart_url" --version "$chart_version" \
    --namespace actions-runner-system -f "$tmpdir/values.yaml" > "$tmpdir/rendered.yaml"
  SET_NAME="ghar-set-${set}" yq \
    'select(.kind == "AutoscalingRunnerSet" and .metadata.name == strenv(SET_NAME))' \
    "$tmpdir/rendered.yaml" > "$tmpdir/runner-set.yaml"
  if ! check_capacity "$tmpdir/runner-set.yaml"; then
    printf 'ghar-set-%s must retain maxRunners >= 8 and minRunners 0; disk saturation is not permission to throttle CI\n' "$set" >&2
    exit 1
  fi

  # Prove the guard rejects the prior caps and malformed/unset maximums.
  for cap in 0 1 2 4 7; do
    CAP="$cap" yq '.spec.maxRunners = env(CAP)' "$tmpdir/runner-set.yaml" > "$tmpdir/reduced.yaml"
    if check_capacity "$tmpdir/reduced.yaml" 2>/dev/null; then
      printf 'capacity guard accepted forbidden maximum %s\n' "$cap" >&2
      exit 1
    fi
  done
  for expression in 'del(.spec.maxRunners)' '.spec.maxRunners = "8"'; do
    yq "$expression" "$tmpdir/runner-set.yaml" > "$tmpdir/invalid.yaml"
    if check_capacity "$tmpdir/invalid.yaml" 2>/dev/null; then
      printf 'capacity guard accepted invalid maximum: %s\n' "$expression" >&2
      exit 1
    fi
  done
  yq '.spec.maxRunners = 9' "$tmpdir/runner-set.yaml" > "$tmpdir/increased.yaml"
  check_capacity "$tmpdir/increased.yaml"
done
printf 'ok: Zoo and MaudeCode rendered capacity floors, reduction rejection, and higher-capacity acceptance\n'
