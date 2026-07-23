#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

tmpdir=$(mktemp -d)
test_file=$(mktemp "${repo_root}/talos/clusterconfig/.hermes-guard-test.XXXXXX")
test_file_relative=${test_file#"${repo_root}/"}
cleanup() {
  rm -rf "$tmpdir"
  rm -f "$test_file"
}
trap cleanup EXIT

scripts/check-generated-secrets

index_file="${tmpdir}/index"
cp "$(git rev-parse --git-path index)" "$index_file"
GIT_INDEX_FILE="$index_file" git add -f "$test_file_relative"

if GIT_INDEX_FILE="$index_file" scripts/check-generated-secrets >/dev/null 2>&1; then
  echo 'generated credential guard accepted a force-tracked Talos config' >&2
  exit 1
fi

corrupt_index="${tmpdir}/corrupt-index"
printf 'not a git index\n' > "$corrupt_index"
if output=$(GIT_INDEX_FILE="$corrupt_index" scripts/check-generated-secrets 2>&1); then
  echo 'generated credential guard accepted a corrupt Git index' >&2
  exit 1
fi
if grep -Fq 'ok: generated credentials' <<< "$output"; then
  echo 'generated credential guard reported success after a Git index failure' >&2
  exit 1
fi

printf 'ok: generated credential guard fails closed for tracked output and Git errors\n'
