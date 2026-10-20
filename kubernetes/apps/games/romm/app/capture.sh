#!/bin/bash
# Existing native PG17 capture, paired with the effective RomM authentication key.
set -euo pipefail
umask 077
root=/config/.kopiur-postgres
[ ! -L "$root" ] || exit 1
mkdir -p "$root"
[ ! -L "$root/romm.lock" ] || exit 1
exec 9>"$root/romm.lock"
flock -n 9 || exit 1
rm -f "$root/COMPLETE"
rm -rf "$root/pending"
# Fail before allocating a native archive if the production volume is too full.
used=$(du -sx --block-size=1 --exclude=.kopiur-postgres /config | cut -f1)
available=$(df -B1 --output=avail /config | tail -n 1 | tr -d ' ')
capacity=$(df -B1 --output=size /config | tail -n 1 | tr -d ' ')
export PGCONNECT_TIMEOUT=10 PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=30000'
dbbytes=$(psql -X -A -t -w -v ON_ERROR_STOP=1 -d romm -c 'SELECT pg_database_size(current_database())')
case "$dbbytes" in ''|*[!0-9]*) exit 1 ;; esac
[ "$((available - used - dbbytes * 2))" -gt "$((capacity / 10 + 134217728))" ]
key_before=$(sha256sum /runtime/ROMM_AUTH_SECRET_KEY)
sh /kopiur-postgres/capture.sh
# A hook succeeds only after key escrow is durable and checksummed too.
rm -f "$root/COMPLETE"
trap 'rm -f "$root/COMPLETE"' HUP INT TERM
[ -s /runtime/ROMM_AUTH_SECRET_KEY ] && [ -s /runtime/config.yml ]
[ "$key_before" = "$(sha256sum /runtime/ROMM_AUTH_SECRET_KEY)" ]
cp /runtime/ROMM_AUTH_SECRET_KEY "$root/current/ROMM_AUTH_SECRET_KEY"
cmp /runtime/config.yml "$root/current/application-config" >/dev/null
(cd "$root/current" && sha256sum ROMM_AUTH_SECRET_KEY >>SHA256SUMS && sha256sum -c SHA256SUMS >/dev/null)
sync -f "$root/current"
printf 'complete\n' >"$root/COMPLETE"
sync -f "$root"
