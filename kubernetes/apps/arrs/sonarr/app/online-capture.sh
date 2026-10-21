#!/bin/sh
# Online native PG17 transaction plus config, followed by the ordinary CSI point.
set -eu
umask 077
root=/config/.kopiur-postgres
[ ! -L "$root" ] || exit 1
mkdir -p "$root"
[ ! -L "$root/lock" ] || exit 1
exec 9>"$root/lock"
flock -n 9 || { printf 'native capture already running\n' >&2; exit 1; }
rm -f "$root/COMPLETE"
for value in "$PGHOST" "$PGUSER" "$PGPASSWORD" "$PGDATABASES" "$CONFIG_FILE"; do
  [ -n "$value" ] || exit 1
done
[ "$PGDATABASES" = sonarr_main ]
[ -f "$CONFIG_FILE" ] && [ ! -L "$CONFIG_FILE" ] && [ -s "$CONFIG_FILE" ]
rm -rf "$root/pending"
mkdir -m 700 "$root/pending"
trap 'rm -rf "$root/pending"' EXIT
trap 'exit 1' HUP INT TERM
export PGCONNECT_TIMEOUT=10 PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=540000'
flags=$(psql -XAtw -v ON_ERROR_STOP=1 -d sonarr_main -c \
  'SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls FROM pg_roles WHERE rolname=current_user')
[ "$flags" = f ] || { printf 'refusing elevated capture identity\n' >&2; exit 1; }
version=$(psql -XAtw -v ON_ERROR_STOP=1 -d sonarr_main -c 'SHOW server_version_num')
[ "$version" -ge 170000 ] && [ "$version" -lt 180000 ]
# Only actual configuration inputs must remain stable, never a global writer or
# whole file-tree quiet period. Native pg_dump uses one read-only transaction.
cp "$CONFIG_FILE" "$root/pending/application-config"
pg_dump -w --format=custom --no-owner --no-privileges --lock-wait-timeout=30s \
  --file="$root/pending/sonarr_main.dump" --dbname=sonarr_main 2>"$root/pending/pg.stderr" || {
  printf 'online native PostgreSQL export failed; private details withheld\n' >&2; exit 1;
}
rm "$root/pending/pg.stderr"
pg_restore --list "$root/pending/sonarr_main.dump" >"$root/pending/sonarr_main.toc"
test -s "$root/pending/sonarr_main.toc"
cmp -s "$CONFIG_FILE" "$root/pending/application-config" || {
  printf 'actual configuration changed during native capture\n' >&2; exit 1;
}
printf 'format=3\napplication=sonarr\nconsistency=online-native-pg17-transaction-before-csi\nserver_version=%s\ncompleted_at=%s\n' \
  "$version" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$root/pending/metadata"
(cd "$root/pending" && sha256sum sonarr_main.dump sonarr_main.toc application-config metadata >SHA256SUMS && sha256sum -c SHA256SUMS >/dev/null)
sync -f "$root/pending"
# Rotate helper-owned materialization only, never repository backup histories.
rm -rf "$root/previous"
if [ -d "$root/current" ]; then mv "$root/current" "$root/previous"; fi
mv "$root/pending" "$root/current"
sync -f "$root"
printf 'complete\n' >"$root/COMPLETE.tmp"
sync -f "$root/COMPLETE.tmp"
mv "$root/COMPLETE.tmp" "$root/COMPLETE"
sync -f "$root"
printf 'online native PostgreSQL recovery bundle complete, no application hold\n'
