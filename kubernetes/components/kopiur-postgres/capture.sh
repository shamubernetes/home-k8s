#!/bin/sh
# Native per-database PG17 archives plus config, published only as one complete set.
set -eu
umask 077
root=/config/.kopiur-postgres
[ ! -L "$root" ] || exit 1
mkdir -p "$root"
chmod 700 "$root"
[ ! -L "$root/lock" ] || exit 1
exec 9>"$root/lock"
flock -n 9 || { printf 'capture already running\n' >&2; exit 1; }
# Invalidate success before validation. A failed retry can never accept an old dump.
rm -f "$root/COMPLETE"
# Bare variables keep Flux postBuild substitution out of runtime credentials.
for value in "$PGHOST" "$PGUSER" "$PGPASSWORD" "$PGDATABASES" "$CONFIG_FILE"; do
  [ -n "$value" ] || exit 1
done
config=$CONFIG_FILE
[ -f "$config" ] && [ ! -L "$config" ] && [ -s "$config" ]
# These directories are solely owned by this helper, never application state.
rm -rf "$root/pending" "$root/previous"
mkdir "$root/pending"
trap 'rm -rf "$root/pending"' EXIT
trap 'exit 1' HUP INT TERM
export PGCONNECT_TIMEOUT=10 PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=540000'
started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf 'format=1\nstarted_at=%s\nconsistency=per-database-snapshot-before-pvc\n' "$started" >"$root/pending/metadata"
# Refuse connection-string syntax and unsafe artifact names in database arguments.
for database in $PGDATABASES; do
  case "$database" in ''|*[!a-z0-9_]*) exit 1 ;; esac
  flags=$(psql -X -A -t -w -v ON_ERROR_STOP=1 -d "$database" -c \
    "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
  [ "$flags" = f ] || { printf 'refusing elevated capture identity\n' >&2; exit 1; }
  version=$(psql -X -A -t -w -v ON_ERROR_STOP=1 -d "$database" -c 'SHOW server_version_num')
  [ "$version" -ge 170000 ] && [ "$version" -lt 180000 ]
  tables=$(psql -X -A -t -w -v ON_ERROR_STOP=1 -d "$database" -c \
    "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')")
  [ "$tables" -gt 0 ] || { printf 'refusing empty schema\n' >&2; exit 1; }
  pg_dump -w --format=custom --no-owner --no-privileges --lock-wait-timeout=30s \
    --file="$root/pending/$database.dump" --dbname="$database"
  pg_restore --list "$root/pending/$database.dump" >"$root/pending/$database.toc"
  [ -s "$root/pending/$database.dump" ] && [ -s "$root/pending/$database.toc" ]
  printf 'database=%s server_version=%s tables=%s\n' "$database" "$version" "$tables" >>"$root/pending/metadata"
done
cp "$config" "$root/pending/application-config"
printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$root/pending/metadata"
(cd "$root/pending" && sha256sum ./*.dump ./*.toc application-config metadata >SHA256SUMS && sha256sum -c SHA256SUMS >/dev/null)
sync -f "$root/pending"
if [ -d "$root/current" ]; then mv "$root/current" "$root/previous"; fi
mv "$root/pending" "$root/current"
sync -f "$root"
printf 'complete\n' >"$root/COMPLETE.tmp"
sync -f "$root/COMPLETE.tmp"
mv "$root/COMPLETE.tmp" "$root/COMPLETE"
sync -f "$root"
printf 'native PostgreSQL recovery bundle complete\n'
