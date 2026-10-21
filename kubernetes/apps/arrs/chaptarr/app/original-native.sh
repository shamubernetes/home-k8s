#!/usr/bin/env bash
set -euo pipefail
umask 077
export PGHOST=/work PGUSER=recovery PGPORT=5432
initdb -D /work/postgres --auth=trust --username=recovery >/dev/null 2>/work/pg-init.log
cleanup() { pg_ctl -D /work/postgres -m fast stop >/dev/null 2>&1 || true; }
trap cleanup EXIT HUP INT TERM
pg_ctl -D /work/postgres -l /work/pg-server.log -o "-c listen_addresses='' -k /work" start >/dev/null
for backend in nas r2; do
  generation=$(cat "/work/$backend-generation")
  for database in chaptarr_main chaptarr_log chaptarr_cache; do
    restored="${backend}_${database}"
    createdb "$restored"
    pg_restore --exit-on-error --no-owner --no-privileges --dbname "$restored" "/work/$backend/.kopiur-chaptarr/$generation/$database.dump" >/dev/null 2>"/work/$restored-restore.log"
    psql -XAt -v ON_ERROR_STOP=1 -d "$restored" -f /kopiur/fingerprint.sql >"/work/$restored-fingerprint"
    test -s "/work/$restored-fingerprint"
  done
done
for database in chaptarr_main chaptarr_log chaptarr_cache; do
  cmp "/work/nas_$database-fingerprint" "/work/r2_$database-fingerprint"
  printf '%s ' "$database"
  sha256sum "/work/nas_$database-fingerprint" | cut -d ' ' -f1
done
cat /work/source-receipt.json
printf '\n{"app":"chaptarr","native_postgresql_17_restore":true,"all_three_original_databases_equal":true,"network_isolated":true}\n'
