#!/bin/bash
# Executed in the existing MariaDB sidecar. No root credential required.
set -euo pipefail
umask 077
if [[ ${1:-} != --bounded ]]; then
  exec timeout --signal=TERM --kill-after=5s 180s bash "$0" --bounded
fi
: "${MYSQL_USER:?missing application database user}"
: "${MYSQL_PASSWORD:?missing application database password}"
[[ ${MYSQL_DATABASE:-grimmory} == grimmory ]] || exit 1
export MYSQL_PWD="$MYSQL_PASSWORD"
root=${GRIMMORY_BACKUP_DIR:-/config/kopiur}
mkdir -p "$root"
exec 9>"$root/.capture.lock"
flock -n 9 || { printf 'capture already running\n' >&2; exit 1; }
# A failed run must never leave a current-looking dump for a later mover.
rm -f "$root/grimmory.sql" "$root/grimmory.sql.sha256" "$root/manifest.txt"
stage=$(mktemp -d "$root/.capture.XXXXXX")
trap 'rm -rf -- "$stage"' EXIT
client=(mariadb --protocol=tcp --host=127.0.0.1 --port=3306 --user="$MYSQL_USER" --connect-timeout=5 --batch --skip-column-names)
# No DDL during the transaction. Reject engines without transaction isolation.
engines=$("${client[@]}" -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='grimmory' AND table_type='BASE TABLE' AND engine <> 'InnoDB'")
[[ $engines == 0 ]] || { printf 'non-InnoDB tables refuse transactional capture\n' >&2; exit 1; }
tables=$("${client[@]}" -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='grimmory' AND table_type='BASE TABLE'")
[[ $tables -ge ${GRIMMORY_MIN_TABLES:-73} ]] || { printf 'table baseline failed\n' >&2; exit 1; }
version=$("${client[@]}" -e 'SELECT VERSION()')
[[ $version == 11.8.8-* ]] || { printf 'MariaDB version changed; requalify restore\n' >&2; exit 1; }
started=$(date -u +%FT%TZ)
# Do not suppress routines/events or retry with an incomplete dump on permission errors.
mariadb-dump --protocol=tcp --host=127.0.0.1 --port=3306 --user="$MYSQL_USER" \
  --single-transaction --quick --skip-lock-tables \
  --routines --events --triggers --hex-blob --databases grimmory >"$stage/grimmory.sql"
[[ -s $stage/grimmory.sql ]] || exit 1
(cd "$stage" && sha256sum grimmory.sql >grimmory.sql.sha256)
printf 'format=mariadb-logical-v1\nserver=%s\nbase_tables=%s\nstarted_utc=%s\ncaptured_utc=%s\nconsistency=single-transaction-no-concurrent-DDL\n' \
  "$version" "$tables" "$started" "$(date -u +%FT%TZ)" >"$stage/manifest.txt"
chmod 640 "$stage/"*
sync
mv "$stage/grimmory.sql" "$stage/grimmory.sql.sha256" "$stage/manifest.txt" "$root/"
sync
printf 'Grimmory transaction dump complete; tables=%s\n' "$tables"
