#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
mkdir -p "${tmpdir}/bin" "${tmpdir}/output" "${tmpdir}/cache"

cat > "${tmpdir}/bin/git" <<'SH'
#!/usr/bin/env bash
exit 42
SH
cat > "${tmpdir}/bin/openapi2jsonschema2" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'run\n' >> "${GENERATOR_CALLS}"
out=''
while (( $# > 0 )); do
  if [[ $1 == -o ]]; then
    out=$2
    shift 2
  else
    shift
  fi
done
[[ -n $out ]] || exit 43
case "${GENERATOR_MODE:-empty}" in
  empty)
    exit 0
    ;;
  valid)
    mkdir -p "$out"
    printf '{"definitions":{}}\n' > "${out}/_definitions.json"
    ;;
  fail)
    exit 44
    ;;
  *)
    exit 45
    ;;
esac
SH
chmod +x "${tmpdir}/bin/git" "${tmpdir}/bin/openapi2jsonschema2"

common_env=(
  "PATH=${tmpdir}/bin:${PATH}"
  "GITHUB_WORKSPACE=${repo_root}"
  "SCHEMA_OUTPUT_DIR=${tmpdir}/output"
  "SCHEMA_CACHE_DIR=${tmpdir}/cache"
  "GENERATOR_CALLS=${tmpdir}/generator-calls"
)

if env "${common_env[@]}" \
  .github/workflows/resources/k8s-schemas.sh >/dev/null 2>&1; then
  echo 'schema generator accepted failed Kubernetes version discovery' >&2
  exit 1
fi
if [[ -e ${tmpdir}/generator-calls ]]; then
  echo 'schema generation ran after Kubernetes version discovery failed' >&2
  exit 1
fi

if env "${common_env[@]}" \
  GENERATOR_MODE=empty \
  K8S_SCHEMA_VERSIONS='v1.36.2' \
  .github/workflows/resources/k8s-schemas.sh >/dev/null 2>&1; then
  echo 'schema generator accepted missing schema output' >&2
  exit 1
fi
if [[ -e ${tmpdir}/cache/v1.36.2.complete ]]; then
  echo 'schema generator cached incomplete output' >&2
  exit 1
fi

# A stale marker without the sentinel must not suppress regeneration.
touch "${tmpdir}/cache/v1.36.2.complete"
: > "${tmpdir}/generator-calls"
env "${common_env[@]}" \
  GENERATOR_MODE=valid \
  K8S_SCHEMA_VERSIONS='v1.36.2' \
  .github/workflows/resources/k8s-schemas.sh >/dev/null
if [[ ! -s ${tmpdir}/output/v1.36.2/_definitions.json ]]; then
  echo 'schema generator did not produce the required sentinel' >&2
  exit 1
fi
if [[ ! -f ${tmpdir}/cache/v1.36.2.complete ]]; then
  echo 'schema generator did not cache complete output' >&2
  exit 1
fi
if [[ $(wc -l < "${tmpdir}/generator-calls" | tr -d ' ') != 2 ]]; then
  echo 'schema generator did not execute both generation passes' >&2
  exit 1
fi

# Complete cached output must skip regeneration.
: > "${tmpdir}/generator-calls"
env "${common_env[@]}" \
  GENERATOR_MODE=fail \
  K8S_SCHEMA_VERSIONS='v1.36.2' \
  .github/workflows/resources/k8s-schemas.sh >/dev/null
if [[ -s ${tmpdir}/generator-calls ]]; then
  echo 'schema generator ignored a valid completion cache' >&2
  exit 1
fi

printf 'ok: Kubernetes schema discovery and content-backed cache fail closed\n'
