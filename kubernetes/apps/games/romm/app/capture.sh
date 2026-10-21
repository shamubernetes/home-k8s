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
[ "$PGDATABASES" = romm ] && [ "$CAPTURE_MODE" = single-db-online-csi-state ]
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
sha256sum /runtime/config.yml /runtime/ROMM_AUTH_SECRET_KEY >"$root/pending/runtime.before"
# Copy key/config before the transaction. Any projection change invalidates it.
cp /runtime/config.yml "$root/pending/application-config"
cp /runtime/ROMM_AUTH_SECRET_KEY "$root/pending/ROMM_AUTH_SECRET_KEY"
pg_dump -w --format=custom --no-owner --no-privileges --lock-wait-timeout=30s --file="$root/pending/romm.dump" --dbname=romm
pg_restore --list "$root/pending/romm.dump" >"$root/pending/romm.toc"
[ -s "$root/pending/romm.dump" ] && [ -s "$root/pending/romm.toc" ]
sha256sum -c "$root/pending/runtime.before" >/dev/null
cmp /runtime/config.yml "$root/pending/application-config" >/dev/null
cmp /runtime/ROMM_AUTH_SECRET_KEY "$root/pending/ROMM_AUTH_SECRET_KEY" >/dev/null
rm "$root/pending/runtime.before"
printf 'format=3\napplication=romm\nrequired_state=complete-csi-root\nconsistency=single-db-online-transaction\nstarted_at=%s\ndatabase=romm server_version=%s tables=%s\ncompleted_at=%s\n' "$started" "$version" "$tables" "$(date -u +%FT%TZ)" >"$root/pending/metadata"
(cd "$root/pending" && sha256sum romm.dump romm.toc application-config ROMM_AUTH_SECRET_KEY metadata >SHA256SUMS && sha256sum -c SHA256SUMS >/dev/null)
sync -f "$root/pending"
rm -rf "$root/previous"
if [ -d "$root/current" ]; then mv "$root/current" "$root/previous"; fi
mv "$root/pending" "$root/current"
sync -f "$root"
printf 'complete\n' >"$root/COMPLETE.tmp"
sync -f "$root/COMPLETE.tmp"
mv "$root/COMPLETE.tmp" "$root/COMPLETE"
sync -f "$root"
