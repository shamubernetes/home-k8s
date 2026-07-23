#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
tmp=$(mktemp -d)
trap 'rm -rf "${tmp}"' EXIT

fake="${tmp}/flate"
baseline="${tmp}/baseline.tsv"

# The quoted lines are the literal body of the generated fake executable.
# shellcheck disable=SC2016
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'printf "  ✗  HelmRelease     test/example  expected offline failure\\n"' \
  'case "${FAKE_MODE:-expected}" in' \
  '  expected) exit 1 ;;' \
  '  unexpected) printf "  ✗  HelmRelease     test/new  new failure\\n"; exit 1 ;;' \
  '  success) exit 0 ;;' \
  '  secret) printf "ENC[AES256_GCM,data:must-not-leak]\\n"; exit 1 ;;' \
  '  tool-error) exit 2 ;;' \
  'esac' > "${fake}"
chmod +x "${fake}"
printf 'failed\tHelmRelease\ttest/example\texpected offline failure\n' > "${baseline}"

run_check() {
  FAKE_MODE=$1 FLATE_BIN=${fake} FLATE_BASELINE=${baseline} FLATE_PATH=${tmp} \
    "${repo_root}/scripts/check-flate-render" >/dev/null 2>&1
}

run_check expected

for mode in unexpected success secret tool-error; do
  if run_check "${mode}"; then
    printf 'Flate baseline check accepted mode %s\n' "${mode}" >&2
    exit 1
  fi
done

printf 'failed\tHelmRelease\ttest/stale\texpected offline failure\n' > "${baseline}"
if run_check expected; then
  echo 'Flate baseline check accepted a stale baseline' >&2
  exit 1
fi

echo 'ok: Flate exception baseline fails closed'
