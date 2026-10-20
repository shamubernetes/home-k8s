#!/bin/bash
# One PG17 transaction plus stable assets/config and the original auth key.
set -euo pipefail
umask 077
root=/config/.kopiur-postgres
[ ! -L "$root" ] || exit 1
mkdir -p "$root"
chmod 700 "$root"
[ ! -L "$root/lock" ] || exit 1
exec 9>"$root/lock"
flock -n 9 || exit 1
rm -f "$root/COMPLETE" "$root/COMPLETE.tmp"
[ "$PGDATABASES" = romm ] && [ "$CAPTURE_MODE" = single-db-stable-filetree ]
for value in "$PGHOST" "$PGUSER" "$PGPASSWORD" "$CONFIG_FILE"; do [ -n "$value" ]; done
[ "$CONFIG_FILE" = /runtime/config.yml ]
for file in /runtime/config.yml /runtime/ROMM_AUTH_SECRET_KEY; do
  [ -s "$file" ] && [ ! -L "$file" ]
done
rm -rf "$root/pending"
mkdir "$root/pending"
trap 'rm -rf "$root/pending"' EXIT
trap 'exit 1' HUP INT TERM
export PGCONNECT_TIMEOUT=10 PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=540000'
flags=$(psql -XAtw -v ON_ERROR_STOP=1 -d romm -c 'SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls FROM pg_roles WHERE rolname=current_user')
[ "$flags" = f ] || exit 1
version=$(psql -XAtw -v ON_ERROR_STOP=1 -d romm -c 'SHOW server_version_num')
[ "$version" -ge 170000 ] && [ "$version" -lt 180000 ]
tables=$(psql -XAtw -v ON_ERROR_STOP=1 -d romm -c "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')")
[ "$tables" -gt 0 ] || exit 1
started=$(date -u +%FT%TZ)
used=$(du -sx --block-size=1 --exclude=.kopiur-postgres /config | cut -f1)
read -r available capacity < <(df -B1 --output=avail,size /config | tail -n 1)
dbbytes=$(psql -XAtw -v ON_ERROR_STOP=1 -d romm -c 'SELECT pg_database_size(current_database())')
case "$dbbytes" in ''|*[!0-9]*) exit 1 ;; esac
[ "$((available - used - dbbytes * 2))" -gt "$((capacity / 10 + 134217728))" ]
qualify() {
  find /config -path "$root" -prune -o -printf '%D\n' >"$root/pending/devices"
  [ "$(sort -u "$root/pending/devices")" = "$(stat -c %d /config)" ]
  find /config -path "$root" -prune -o \( ! -type f ! -type d -o -type f -links +1 \) -print -quit >"$root/pending/unsupported"
  [ ! -s "$root/pending/unsupported" ]
}
inventory() {
  find /config -path "$root" -prune -o -printf '%P/%y/%D/%i/%n/%m/%U/%G/%s/%T@/%C@\0' | sort -z
}
qualify
inventory >"$root/pending/filetree.before"
sha256sum /runtime/config.yml /runtime/ROMM_AUTH_SECRET_KEY >"$root/pending/runtime.before"
# Copy key/config before the transaction. Any projection change invalidates it.
cp /runtime/config.yml "$root/pending/application-config"
cp /runtime/ROMM_AUTH_SECRET_KEY "$root/pending/ROMM_AUTH_SECRET_KEY"
pg_dump -w --format=custom --no-owner --no-privileges --lock-wait-timeout=30s --file="$root/pending/romm.dump" --dbname=romm
pg_restore --list "$root/pending/romm.dump" >"$root/pending/romm.toc"
[ -s "$root/pending/romm.dump" ] && [ -s "$root/pending/romm.toc" ]
tar --create --one-file-system --numeric-owner --file="$root/pending/application-state.tar" --exclude='./.kopiur-postgres' --directory=/config .
inventory >"$root/pending/filetree.after"
cmp -s "$root/pending/filetree.before" "$root/pending/filetree.after"
qualify
sha256sum -c "$root/pending/runtime.before" >/dev/null
cmp /runtime/config.yml "$root/pending/application-config" >/dev/null
cmp /runtime/ROMM_AUTH_SECRET_KEY "$root/pending/ROMM_AUTH_SECRET_KEY" >/dev/null
sha256sum "$root/pending/filetree.before" | cut -d ' ' -f1 >"$root/pending/filetree.sha256"
rm "$root/pending/"{filetree.before,filetree.after,runtime.before,devices,unsupported}
printf 'format=2\napplication=romm\nconsistency=single-db-stable-filetree\nstarted_at=%s\ndatabase=romm server_version=%s tables=%s\ncompleted_at=%s\n' "$started" "$version" "$tables" "$(date -u +%FT%TZ)" >"$root/pending/metadata"
(cd "$root/pending" && sha256sum romm.dump romm.toc application-config application-state.tar ROMM_AUTH_SECRET_KEY filetree.sha256 metadata >SHA256SUMS && sha256sum -c SHA256SUMS >/dev/null)
sync -f "$root/pending"
rm -rf "$root/previous"
if [ -d "$root/current" ]; then mv "$root/current" "$root/previous"; fi
mv "$root/pending" "$root/current"
sync -f "$root"
printf 'complete\n' >"$root/COMPLETE.tmp"
sync -f "$root/COMPLETE.tmp"
mv "$root/COMPLETE.tmp" "$root/COMPLETE"
sync -f "$root"
