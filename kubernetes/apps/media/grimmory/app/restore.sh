#!/bin/bash
# Run only in a new isolated MariaDB sidecar, before starting the app.
set -euo pipefail
if [[ ${1:-} != --bounded ]]; then
  exec timeout --signal=TERM --kill-after=5s 300s bash "$0" --bounded "$@"
fi
shift
[[ ${K8S92_ISOLATED_RESTORE:-} == YES ]] || exit 1
bundle=${1:?pass restored dump directory}
: "${MYSQL_USER:?missing restore database user}"
: "${MYSQL_PASSWORD:?missing fresh restore password}"
export MYSQL_PWD="$MYSQL_PASSWORD"
client=(mariadb --protocol=tcp --host=127.0.0.1 --port=3306 --user="$MYSQL_USER" --connect-timeout=5 --batch --skip-column-names)
[[ $("${client[@]}" -e 'SELECT VERSION()') == 11.8.8-* ]] || exit 1
[[ $("${client[@]}" -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='grimmory'") == 0 ]] || { printf 'refuse nonempty target\n' >&2; exit 1; }
(cd "$bundle" && sha256sum -c grimmory.sql.sha256)
# Preserve legacy v1 restores. New v2 points must carry the bound runtime escrow.
format=$(sed -n 's/^format=//p' "$bundle/manifest.txt")
case "$format" in
  mariadb-logical-v1) ;;
  mariadb-logical-runtime-v2)
    generation=$(sed -n 's/^runtime_generation=//p' "$bundle/manifest.txt")
    [[ $generation =~ ^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$ ]]
    [[ $(cat "$bundle/COMPLETE") == "$generation" ]]
    [[ $(sed -n 's/^generation=//p' "$bundle/runtime/runtime.txt") == "$generation" ]]
    digest=$(sed -n 's/^runtime_manifest_sha256=//p' "$bundle/manifest.txt")
    [[ $digest =~ ^[0-9a-f]{64}$ ]]
    [[ $(sha256sum "$bundle/runtime/runtime.sha256" | cut -d' ' -f1) == "$digest" ]]
    sh "$(dirname "$0")/runtime.sh" --bounded validate "$bundle/runtime"
    sql_digest=$(sed -n 's/^sql_sha256=//p' "$bundle/manifest.txt")
    [[ $sql_digest =~ ^[0-9a-f]{64}$ ]]
    [[ $(sha256sum "$bundle/grimmory.sql" | cut -d' ' -f1) == "$sql_digest" ]]
    ;;
  *) exit 1 ;;
esac
expected=
while IFS='=' read -r key value; do
  if [[ $key == base_tables ]]; then expected=$value; fi
done <"$bundle/manifest.txt"
[[ $expected =~ ^[0-9]+$ && $expected -ge 73 ]] || exit 1
"${client[@]}" <"$bundle/grimmory.sql"
actual=$("${client[@]}" -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='grimmory' AND table_type='BASE TABLE'")
[[ $actual == "$expected" ]] || exit 1
mariadb-check --protocol=tcp --host=127.0.0.1 --port=3306 --user="$MYSQL_USER" --check grimmory >/dev/null
printf 'Fresh MariaDB import and table checks passed; base_tables=%s\n' "$actual"
