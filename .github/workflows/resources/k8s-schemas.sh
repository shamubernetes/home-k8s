#!/usr/bin/env bash

# renovate: datasource=docker depName=ghcr.io/siderolabs/kubelet
K8S_VERSIONS="v1.30.2"
for K8S_VERSION in $K8S_VERSIONS master; do
  SCHEMA=https://raw.githubusercontent.com/kubernetes/kubernetes/${K8S_VERSION}/api/openapi-spec/swagger.json
  PREFIX=https://k8s-schemas.pages.dev/kubernetes/${K8S_VERSION}/_definitions.json

  if [ ! -d "${K8S_VERSION}" ]; then
    openapi2jsonschema -o "/home/runner/schemas/kubernetes/${K8S_VERSION}" --expanded --kubernetes --prefix "${PREFIX}" "${SCHEMA}"
    openapi2jsonschema -o "/home/runner/schemas/kubernetes/${K8S_VERSION}" --kubernetes --prefix "${PREFIX}" "${SCHEMA}"
  fi
done
