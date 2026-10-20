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
rm -f "$root/grimmory.sql" "$root/grimmory.sql.sha256" "$root/manifest.txt" "$root/COMPLETE"
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
runtime=/runtime-escrow
[[ ! -L $runtime && ! -L $runtime/current && -f $runtime/COMPLETE ]] || exit 1
generation=$(cat "$runtime/COMPLETE")
[[ $generation =~ ^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$ ]] || exit 1
[[ $(sed -n 's/^generation=//p' "$runtime/current/runtime.txt") == "$generation" ]] || exit 1
captured=$(sed -n 's/^captured_epoch=//p' "$runtime/current/runtime.txt")
[[ $captured =~ ^[0-9]+$ ]] || exit 1
now=$(date +%s)
# Reuse the existing native capture bound, not a new retention/skew policy.
(( now >= captured && now - captured <= 180 )) || exit 1
sh "$(dirname "$0")/runtime.sh" --bounded validate "$runtime/current"
mkdir "$stage/runtime"
cp "$runtime/current/"* "$stage/runtime/"
(cd "$stage/runtime" && sha256sum -c runtime.sha256 >/dev/null)
started=$(date -u +%FT%TZ)
# Native schema inventories bracket the transaction. They detect observed DDL,
# not an external writer fence. Ignore volatile AUTO_INCREMENT create options.
schema() {
  mariadb-dump --protocol=tcp --host=127.0.0.1 --port=3306 --user="$MYSQL_USER" \
    --no-data --skip-comments --skip-create-options --skip-lock-tables \
    --routines --events --triggers --databases grimmory >"$1"
  [[ -s $1 ]]
}
schema "$stage/schema.before"
# Do not suppress routines/events or retry with an incomplete dump on permission errors.
mariadb-dump --protocol=tcp --host=127.0.0.1 --port=3306 --user="$MYSQL_USER" \
  --single-transaction --quick --skip-lock-tables \
  --routines --events --triggers --hex-blob --databases grimmory >"$stage/grimmory.sql"
[[ -s $stage/grimmory.sql ]] || exit 1
schema "$stage/schema.after"
cmp -s "$stage/schema.before" "$stage/schema.after" || { printf 'schema changed during capture\n' >&2; exit 1; }
[[ $("${client[@]}" -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='grimmory' AND table_type='BASE TABLE' AND engine <> 'InnoDB'") == 0 ]] || exit 1
(cd "$stage" && sha256sum grimmory.sql >grimmory.sql.sha256)
printf 'format=mariadb-logical-runtime-v2\nserver=%s\nbase_tables=%s\nstarted_utc=%s\ncaptured_utc=%s\nconsistency=single-transaction-no-concurrent-DDL\nruntime_generation=%s\nruntime_manifest_sha256=%s\nsql_sha256=%s\n' \
  "$version" "$tables" "$started" "$(date -u +%FT%TZ)" "$generation" "$(sha256sum "$stage/runtime/runtime.sha256" | cut -d' ' -f1)" "$(sha256sum "$stage/grimmory.sql" | cut -d' ' -f1)" >"$stage/manifest.txt"
# Reject changes to the captured runtime while the native SQL transaction ran.
[[ $(cat "$runtime/COMPLETE") == "$generation" ]]
sh "$(dirname "$0")/runtime.sh" --bounded validate "$runtime/current"
cmp -s "$runtime/current/runtime.sha256" "$stage/runtime/runtime.sha256"
chmod 640 "$stage/grimmory.sql" "$stage/grimmory.sql.sha256" "$stage/manifest.txt"
sync
rm -rf -- "$root/runtime"
mv "$stage/runtime" "$root/runtime"
mv "$stage/grimmory.sql" "$stage/grimmory.sql.sha256" "$stage/manifest.txt" "$root/"
printf '%s\n' "$generation" >"$root/COMPLETE"
sync
printf 'Grimmory transaction dump complete; tables=%s\n' "$tables"
