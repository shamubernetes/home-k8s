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
# A single PG transaction can be paired without stopping the application when
# the entire required file tree is unchanged throughout its capture interval.
# Metadata includes inode/ctime, so rewriting then restoring bytes is rejected.
coherent=false
capture_mode=$(printenv CAPTURE_MODE || printf legacy)
case "$capture_mode" in
  single-db-stable-filetree)
    set -- $PGDATABASES
    [ "$#" -eq 1 ] || { printf 'coherent mode requires one database\n' >&2; exit 1; }
    coherent=true
    printf 'format=2\nstarted_at=%s\nconsistency=single-db-stable-filetree\n' "$started" >"$root/pending/metadata"
    ;;
  legacy)
    printf 'format=1\nstarted_at=%s\nconsistency=per-database-snapshot-before-pvc\n' "$started" >"$root/pending/metadata"
    ;;
  *) printf 'unknown capture contract\n' >&2; exit 1 ;;
esac
filetree_inventory() {
  # NUL field boundaries preserve paths with whitespace. Never follow links.
  find /config -path "$root" -prune -o -printf '%P\0%y\0%D\0%i\0%n\0%m\0%U\0%G\0%s\0%T@\0%C@\0'
}
filetree_qualify() {
  # Submounts and special objects cannot be silently skipped by an archive.
  find /config -path "$root" -prune -o -printf '%D\n' >"$root/pending/devices"
  [ "$(sort -u "$root/pending/devices")" = "$(stat -c %d /config)" ]
  find /config -path "$root" -prune -o \( ! -type f ! -type d -o -type f -links +1 \) -print -quit >"$root/pending/unsupported"
  [ ! -s "$root/pending/unsupported" ]
  rm "$root/pending/devices" "$root/pending/unsupported"
}
if "$coherent"; then
  filetree_qualify
  filetree_inventory >"$root/pending/filetree.before"
fi
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
if "$coherent"; then
  tar --create --one-file-system --numeric-owner --file="$root/pending/application-state.tar" \
    --exclude='./.kopiur-postgres' --directory=/config . 2>"$root/pending/archive.stderr" || {
    printf 'application file archive failed\n' >&2; exit 1;
  }
  rm "$root/pending/archive.stderr"
  filetree_inventory >"$root/pending/filetree.after"
  cmp -s "$root/pending/filetree.before" "$root/pending/filetree.after" || {
    printf 'application file state changed during database capture\n' >&2; exit 1;
  }
  filetree_qualify
  sha256sum "$root/pending/filetree.before" >"$root/pending/inventory.checksum"
  cut -d ' ' -f 1 "$root/pending/inventory.checksum" >"$root/pending/filetree.sha256"
  rm "$root/pending/inventory.checksum"
  rm "$root/pending/filetree.before" "$root/pending/filetree.after"
fi
printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$root/pending/metadata"
if "$coherent"; then
  (cd "$root/pending" && sha256sum ./*.dump ./*.toc application-config application-state.tar filetree.sha256 metadata >SHA256SUMS && sha256sum -c SHA256SUMS >/dev/null)
else
  (cd "$root/pending" && sha256sum ./*.dump ./*.toc application-config metadata >SHA256SUMS && sha256sum -c SHA256SUMS >/dev/null)
fi
sync -f "$root/pending"
if [ -d "$root/current" ]; then mv "$root/current" "$root/previous"; fi
mv "$root/pending" "$root/current"
sync -f "$root"
printf 'complete\n' >"$root/COMPLETE.tmp"
sync -f "$root/COMPLETE.tmp"
mv "$root/COMPLETE.tmp" "$root/COMPLETE"
sync -f "$root"
printf 'native PostgreSQL recovery bundle complete\n'
