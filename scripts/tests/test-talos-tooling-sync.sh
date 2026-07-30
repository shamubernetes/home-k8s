#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

scripts/check-talos-tooling-sync >/dev/null

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

cat > "$tmpdir/mise.toml" <<'TOML'
[tools]
talosctl = "1.2.3"
TOML
cat > "$tmpdir/runner.yaml" <<'YAML'
volumes:
- image:
    reference: ghcr.io/siderolabs/talosctl:v1.2.3@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
YAML
scripts/check-talos-tooling-sync \
  --mise "$tmpdir/mise.toml" \
  --runner "$tmpdir/runner.yaml" >/dev/null

cat > "$tmpdir/runner.yaml" <<'YAML'
volumes:
- image:
    reference: ghcr.io/siderolabs/talosctl:v1.2.4@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
YAML
if scripts/check-talos-tooling-sync \
  --mise "$tmpdir/mise.toml" \
  --runner "$tmpdir/runner.yaml" >/dev/null 2>&1; then
  echo 'mismatched Talosctl versions were accepted' >&2
  exit 1
fi

cat > "$tmpdir/runner.yaml" <<'YAML'
volumes:
- image:
    reference: ghcr.io/siderolabs/talosctl:v1.2.3
YAML
if scripts/check-talos-tooling-sync \
  --mise "$tmpdir/mise.toml" \
  --runner "$tmpdir/runner.yaml" >/dev/null 2>&1; then
  echo 'mutable Talosctl reference was accepted' >&2
  exit 1
fi

cat > "$tmpdir/runner.yaml" <<'YAML'
volumes:
- image:
    reference: ghcr.io/siderolabs/talosctl:v1.2.3@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
- image:
    reference: ghcr.io/siderolabs/talosctl:v1.2.3@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
YAML
if scripts/check-talos-tooling-sync \
  --mise "$tmpdir/mise.toml" \
  --runner "$tmpdir/runner.yaml" >/dev/null 2>&1; then
  echo 'duplicate Talosctl references were accepted' >&2
  exit 1
fi

grep -Fq 'Track the Talosctl OCI image volume used by the trusted runner' \
  .github/renovate/customManagers.json5
grep -Fq 'Keep Mise and trusted-runner Talosctl releases synchronized' \
  .github/renovate/packageRules.json5
grep -Fq '"matchDepNames"' .github/renovate/packageRules.json5

printf 'ok: Talosctl tooling synchronization fails closed\n'
