#!/usr/bin/env bash

OPENAPI2JSONSCHEMABIN="docker run -i -v ${PWD}/k8sSchemas:/out/schemas ghcr.io/yannh/openapi2jsonschema:latest"

# renovate: datasource=docker depName=ghcr.io/siderolabs/kubelet
K8S_VERSIONS="v1.30.1"
for K8S_VERSION in $K8S_VERSIONS master; do
  SCHEMA=https://raw.githubusercontent.com/kubernetes/kubernetes/${K8S_VERSION}/api/openapi-spec/swagger.json
  PREFIX=https://k8s-schemas.pages.dev/kubernetes/${K8S_VERSION}/_definitions.json

  if [ ! -d "${K8S_VERSION}-standalone-strict" ]; then
    $OPENAPI2JSONSCHEMABIN -o "schemas/${K8S_VERSION}-standalone-strict" --expanded --kubernetes --stand-alone --strict "${SCHEMA}"
    $OPENAPI2JSONSCHEMABIN -o "schemas/${K8S_VERSION}-standalone-strict" --kubernetes --stand-alone --strict "${SCHEMA}"
  fi

  if [ ! -d "${K8S_VERSION}-standalone" ]; then
    $OPENAPI2JSONSCHEMABIN -o "schemas/${K8S_VERSION}-standalone" --expanded --kubernetes --stand-alone "${SCHEMA}"
    $OPENAPI2JSONSCHEMABIN -o "schemas/${K8S_VERSION}-standalone" --kubernetes --stand-alone "${SCHEMA}"
  fi

  if [ ! -d "${K8S_VERSION}-local" ]; then
    $OPENAPI2JSONSCHEMABIN -o "schemas/${K8S_VERSION}-local" --expanded --kubernetes "${SCHEMA}"
    $OPENAPI2JSONSCHEMABIN -o "schemas/${K8S_VERSION}-local" --kubernetes "${SCHEMA}"
  fi

  if [ ! -d "${K8S_VERSION}" ]; then
    $OPENAPI2JSONSCHEMABIN -o "schemas/${K8S_VERSION}" --expanded --kubernetes --prefix "${PREFIX}" "${SCHEMA}"
    $OPENAPI2JSONSCHEMABIN -o "schemas/${K8S_VERSION}" --kubernetes --prefix "${PREFIX}" "${SCHEMA}"
  fi
done
