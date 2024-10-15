#!/usr/bin/env bash

# Get k8s version from system-upgrade-controller

K8S_VERSIONS=$(git ls-remote --refs --tags https://github.com/kubernetes/kubernetes.git | cut -d/ -f3 | grep -e '^v1\.[0-9]\{2\}\.[0-9]\{1,2\}$' | grep -v -e '^v1\.1[0-8]\{1\}' | tail -10)
CURRENT_VERSION=$(yq 'select(document_index == 1).spec.postBuild.substitute.KUBERNETES_VERSION' "$GITHUB_WORKSPACE"/kubernetes/apps/system-upgrade/system-upgrade-controller/ks.yaml)

# Make sure the current version is in the list
if ! echo "$K8S_VERSIONS" | grep -q "$CURRENT_VERSION"; then
  K8S_VERSIONS="$CURRENT_VERSION"$'\n'"$K8S_VERSIONS"
fi

for K8S_VERSION in $K8S_VERSIONS master; do
  SCHEMA=https://raw.githubusercontent.com/kubernetes/kubernetes/${K8S_VERSION}/api/openapi-spec/swagger.json
  PREFIX=https://k8s-schemas.pages.dev/kubernetes/${K8S_VERSION}/_definitions.json

  if [ ! -d "${K8S_VERSION}" ]; then
    openapi2jsonschema -o "/home/runner/schemas/kubernetes/${K8S_VERSION}" --expanded --kubernetes --prefix "${PREFIX}" "${SCHEMA}"
    openapi2jsonschema -o "/home/runner/schemas/kubernetes/${K8S_VERSION}" --kubernetes --prefix "${PREFIX}" "${SCHEMA}"
  fi
done
