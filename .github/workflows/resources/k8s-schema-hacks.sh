#!/usr/bin/env bash

set -o errexit

if [ ! -d /home/runner/.datree/crdSchemas ]; then
  echo "CRD schema directory not found"
  exit 1
fi

if [ ! -d ./.github/workflows/resources/schema-hacks ]; then
  echo "CRD schema hacks directory not found"
  exit 1
fi

runHack() {
  local hack="$1"
  local schemaDir="/home/runner/.datree/crdSchemas"

  echo "Running hack: $(basename "$hack")"

  # shellcheck disable=SC2155
  local hackLength=$(yq '. | length' "$hack")

  for ((i = 0; i < hackLength; i++)); do
    # shellcheck disable=SC2155
    local hackItem=$(yq ".[$i]" "$hack")
    # shellcheck disable=SC2155
    local hackApi=$(echo "$hackItem" | yq '.api')
    # shellcheck disable=SC2155
    local hackVersion=$(echo "$hackItem" | yq '.version')
    # shellcheck disable=SC2155
    local hackKind=$(echo "$hackItem" | yq '.kind')
    # shellcheck disable=SC2155
    local hackPath=$(echo "$hackItem" | yq '.replacePath')
    # shellcheck disable=SC2155
    local hackValue=$(echo "$hackItem" | yq '.value')

    echo "Applying hack for $hackApi/$hackKind/$hackVersion at $hackPath"
    local schemaFile="$schemaDir/${hackApi,,}/${hackKind,,}_${hackVersion,,}.json"
    yq eval -i ".${hackPath} |= ${hackValue}" "$schemaFile"
  done
}

for hack in ./.github/workflows/resources/schema-hacks/*.yaml; do
  runHack "$hack"
done
