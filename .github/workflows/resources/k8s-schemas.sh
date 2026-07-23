#!/usr/bin/env bash
set -euo pipefail

repo_root=${GITHUB_WORKSPACE:-$(git rev-parse --show-toplevel)}
upgrade_file="${repo_root}/kubernetes/apps/system-upgrade/tuppr/upgrades/kubernetesupgrade.yaml"
output_root=${SCHEMA_OUTPUT_DIR:-/home/runner/schemas/kubernetes}
cache_root=${SCHEMA_CACHE_DIR:-${HOME}/.cache/k8s-schema-generator}

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required command: $1" >&2
    exit 1
  }
}
need yq

current_version=$(yq -er '.spec.kubernetes.version' "$upgrade_file")
if [[ ! $current_version =~ ^v1\.[0-9]+\.[0-9]+$ ]]; then
  printf 'invalid Tuppr Kubernetes version: %s\n' "$current_version" >&2
  exit 1
fi

if [[ ${1:-} == '--current-version' ]]; then
  printf '%s\n' "$current_version"
  exit 0
fi
if (( $# > 0 )); then
  printf 'usage: %s [--current-version]\n' "$0" >&2
  exit 1
fi

need openapi2jsonschema2

mkdir -p "$output_root" "$cache_root"
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
versions_file="${tmpdir}/versions"
: > "$versions_file"

if [[ -n ${K8S_SCHEMA_VERSIONS:-} ]]; then
  for version in $K8S_SCHEMA_VERSIONS; do
    printf '%s\n' "$version" >> "$versions_file"
  done
else
  need git
  refs_file="${tmpdir}/refs"
  git ls-remote --refs --tags https://github.com/kubernetes/kubernetes.git > "$refs_file"
  if ! cut -d/ -f3 "$refs_file" \
    | grep -E '^v1\.[0-9]{2}\.[0-9]{1,2}$' \
    | grep -Ev '^v1\.1[0-8]\.' \
    | sort -V \
    | tail -10 > "$versions_file"; then
    echo 'failed to discover released Kubernetes schema versions' >&2
    exit 1
  fi

  if ! grep -Fxq "$current_version" "$versions_file"; then
    printf '%s\n' "$current_version" >> "$versions_file"
  fi
  printf 'master\n' >> "$versions_file"
fi

if [[ ! -s $versions_file ]]; then
  echo 'no Kubernetes schema versions selected' >&2
  exit 1
fi

while IFS= read -r version; do
  [[ -n $version ]] || continue
  if [[ $version != master && ! $version =~ ^v1\.[0-9]+\.[0-9]+$ ]]; then
    printf 'invalid Kubernetes schema version: %s\n' "$version" >&2
    exit 1
  fi

  schema="https://raw.githubusercontent.com/kubernetes/kubernetes/${version}/api/openapi-spec/swagger.json"
  prefix="https://k8s-schemas.pages.dev/kubernetes/${version}/_definitions.json"
  version_output="${output_root}/${version}"
  marker="${cache_root}/${version}.complete"
  sentinel="${version_output}/_definitions.json"

  if [[ $version != master && -f $marker && -s $sentinel ]]; then
    continue
  fi

  rm -f "$marker"
  rm -rf "$version_output"
  mkdir -p "$version_output"
  openapi2jsonschema2 -o "$version_output" --expanded --kubernetes --prefix "$prefix" "$schema"
  openapi2jsonschema2 -o "$version_output" --kubernetes --prefix "$prefix" "$schema"
  if [[ ! -s $sentinel ]]; then
    printf 'schema generation for %s did not produce a nonempty _definitions.json\n' "$version" >&2
    exit 1
  fi
  touch "$marker"
done < "$versions_file"
