#!/bin/bash
# Use Plex's own SQLite build, never the distribution SQLite against Plex data.
set -euo pipefail
trap 'printf "native Plex capture failed at source line %s\n" "$LINENO" >&2' ERR
umask 077
root='/config'
output="$root/.kopiur-native"
[ ! -L "$output" ] || exit 1
mkdir -p "$output"
chmod 700 "$output"
[ ! -L "$output/lock" ] || exit 1
exec 9>"$output/lock"
flock -n 9 || exit 1
rm -f "$output/COMPLETE"
rm -rf "$output/pending"
# Conservatively require space for another full PVC tree and keep 10% free.
used=$(du -sx --block-size=1 --exclude=.kopiur-native "$root" | cut -f1)
read -r available capacity < <(df -B1 --output=avail,size "$root" | tail -n 1)
[ "$((available - used))" -gt "$((capacity / 10 + 134217728))" ] || exit 1
mkdir "$output/pending"
trap 'rm -rf "$output/pending"' EXIT
trap 'exit 1' HUP INT TERM
sqlite='/usr/lib/plexmediaserver/Plex SQLite'
databases='Plug-in Support/Databases'
# Each WAL read snapshot is pinned without blocking writers or restarting backup.
# Do not require global writer quiescence across the two independent databases.
[ -z "$(find "$root/$databases" -maxdepth 1 -type l -print -quit)" ] || exit 1
state_inventory() {
  find "$root" \( -path "$output" -o -path "$root/$databases" -o -path "$root/Cache" -o -path "$root/Media" -o -path "$root/Logs" \) -prune -o -printf '%P/%y/%i/%s/%T@/%C@/%m/%U/%G/%l\0' | sort -z
  # Plex Metadata uses relative links. Preserve them in tar, never dereference
  # outside captured state or accept dangling links and special objects.
  find "$root" \( -path "$output" -o -path "$root/$databases" -o -path "$root/Cache" -o -path "$root/Media" -o -path "$root/Logs" \) -prune -o \( ! -type f ! -type d ! -type l \) -print >"$output/unsupported"
  [ ! -s "$output/unsupported" ] || return 1
  find "$root" \( -path "$output" -o -path "$root/$databases" -o -path "$root/Cache" -o -path "$root/Media" -o -path "$root/Logs" \) -prune -o -type l -print0 >"$output/links"
  while IFS= read -r -d '' link; do
    target=$(readlink "$link") || return 1
    case "$target" in /*) return 1 ;; esac
    resolved=$(readlink -e "$link") || return 1
    case "$resolved" in
      "$output"|"$output/"*|"$root/$databases"|"$root/$databases/"*|"$root/Cache"|"$root/Cache/"*|"$root/Media"|"$root/Media/"*|"$root/Logs"|"$root/Logs/"*) return 1 ;;
      "$root/"*) [ -f "$resolved" ] || return 1 ;;
      *) return 1 ;;
    esac
  done <"$output/links"
  rm "$output/links"
}
state_inventory >"$output/pending/state.before"
for name in com.plexapp.plugins.library.db com.plexapp.plugins.library.blobs.db; do
  source="$root/$databases/$name"
  [ -s "$source" ] && [ ! -L "$source" ]
  [ "$(timeout 10 "$sqlite" -readonly "$source" 'PRAGMA journal_mode;')" = wal ] || exit 1
  started=$(date +%s)
  # BEGIN plus a real schema read anchors the source connection's WAL snapshot.
  # query_only and readonly forbid source writes; concurrent WAL writers continue.
  timeout 240 "$sqlite" -readonly -cmd 'PRAGMA query_only=ON;' -cmd 'BEGIN;' \
    -cmd 'SELECT 1 FROM sqlite_schema LIMIT 1;' "$source" ".backup '$output/pending/$name'" >/dev/null
  printf 'native SQLite WAL backup %s seconds=%s\n' "$name" "$(( $(date +%s) - started ))"
  result=$(timeout 60 "$sqlite" -readonly "$output/pending/$name" 'PRAGMA integrity_check;')
  [ "$result" = ok ] || exit 1
done
# Exclude only regeneratable mounts, logs, DB bytes replaced by native copies,
# and this helper's private tree. All required config, metadata and plugins remain.
[ -s "$root/Preferences.xml" ] && [ ! -L "$root/Preferences.xml" ]
tar --create --one-file-system --numeric-owner --file="$output/pending/state.tar" \
  --exclude='./.kopiur-native' --exclude='./Plug-in Support/Databases' \
  --exclude='./Cache' --exclude='./Media' --exclude='./Logs' --directory="$root" .
state_inventory >"$output/pending/state.after"
cmp "$output/pending/state.before" "$output/pending/state.after" >/dev/null || exit 1
rm "$output/pending/state.before" "$output/pending/state.after"
printf 'format=1\napplication=plex\nconsistency=online-wal-snapshot-per-database\ncompleted_at=%s\n' "$(date -u +%FT%TZ)" >"$output/pending/metadata"
(cd "$output/pending" && sha256sum ./*.db state.tar metadata >SHA256SUMS && sha256sum -c SHA256SUMS >/dev/null)
sync -f "$output/pending"
rm -rf "$output/previous"
if [ -d "$output/current" ]; then mv "$output/current" "$output/previous"; fi
mv "$output/pending" "$output/current"
printf 'complete\n' >"$output/COMPLETE"
sync -f "$output"
