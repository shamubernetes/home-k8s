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
