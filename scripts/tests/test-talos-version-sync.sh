#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
tmp=$(mktemp -d)
trap 'rm -rf "${tmp}"' EXIT

talconfig="${tmp}/talconfig.yaml"
tuppr="${tmp}/talosupgrade.yaml"

printf 'talosVersion: v1.2.3\n' > "${talconfig}"
printf 'spec:\n  talos:\n    version: v1.2.3\n' > "${tuppr}"

TALCONFIG_PATH=${talconfig} TUPPR_PATH=${tuppr} \
  "${repo_root}/scripts/check-talos-version-sync" >/dev/null

printf 'spec:\n  talos:\n    version: v1.2.4\n' > "${tuppr}"
if TALCONFIG_PATH=${talconfig} TUPPR_PATH=${tuppr} \
  "${repo_root}/scripts/check-talos-version-sync" >/dev/null 2>&1; then
  echo 'mismatched Talos versions were accepted' >&2
  exit 1
fi

printf 'talosVersion: v1.2.3\ntalosVersion: v1.2.3\n' > "${talconfig}"
if TALCONFIG_PATH=${talconfig} TUPPR_PATH=${tuppr} \
  "${repo_root}/scripts/check-talos-version-sync" >/dev/null 2>&1; then
  echo 'duplicate Talos versions were accepted' >&2
  exit 1
fi

echo 'ok: Talos version synchronization fails closed'
