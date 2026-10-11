#!/bin/sh
# Root reads the effective Java process, then hands private escrow to mover UID568.
set -eu
umask 077
if [ "${1:-}" != --bounded ]; then
  exec timeout -s TERM -k 5 180 sh "$0" --bounded "$@"
fi
mode=${2:-capture}
if [ "$mode" = validate ]; then
  directory=${3:?pass runtime directory}
  [ -d "$directory" ] && [ ! -L "$directory" ] || exit 1
  expected='application.env0 application.argv0 app-data.tar process-start.txt runtime.txt'
  for name in $expected runtime.sha256; do
    [ -f "$directory/$name" ] && [ ! -L "$directory/$name" ] && [ -s "$directory/$name" ] || exit 1
  done
  # An arbitrary checksum list must not redefine the required recovery payload.
  awk 'BEGIN {n=split("application.env0 application.argv0 app-data.tar process-start.txt runtime.txt",a," "); for(i=1;i<=n;i++) expected[a[i]]=1}
       NF!=2 || length($1)!=64 || $1 !~ /^[0-9a-f]+$/ || !($2 in expected) || seen[$2]++ {exit 1}
       END {if (NR!=5) exit 1}' "$directory/runtime.sha256"
  LC_ALL=C grep -Eq '^[0-9]+$' "$directory/process-start.txt"
  [ "$(sed -n 's/^format=//p' "$directory/runtime.txt")" = grimmory-runtime-v1 ]
  LC_ALL=C grep -Eq '^generation=[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' "$directory/runtime.txt"
  LC_ALL=C grep -Eq '^captured_epoch=[0-9]+$' "$directory/runtime.txt"
  (cd "$directory" && sha256sum -c runtime.sha256 >/dev/null)
  exit
fi
# This is a dedicated PVC subPath mount beneath the root-owned container root,
# not a child of app-writable /app/data. UID568 cannot rename its parent entry.
root=/runtime-escrow
proc=${GRIMMORY_PROC_ROOT:-/proc}/1
[ ! -L "$root" ] || exit 1
mkdir -p "$root"
# The native app UID must not redirect a root control-file write.
chown 0:568 "$root"
chmod 750 "$root"
for name in .lock COMPLETE current previous; do
  [ ! -L "$root/$name" ] || exit 1
done
[ ! -e "$root/.lock" ] || [ -f "$root/.lock" ]
[ ! -e "$root/COMPLETE" ] || [ -f "$root/COMPLETE" ]
[ ! -e "$root/current" ] || [ -d "$root/current" ]
[ ! -e "$root/previous" ] || [ -d "$root/previous" ]
exec 9>"$root/.lock"
chown 0:568 "$root/.lock"
chmod 640 "$root/.lock"
flock -n 9 || { printf 'runtime capture already running\n' >&2; exit 1; }
[ "$(cat "$proc/comm")" = java ] || exit 1
stage=$(mktemp -d "$root/.capture.XXXXXX")
trap 'rm -rf -- "$stage"' EXIT
# Do not echo any environment, command arguments or configuration content.
copy_effective() {
  cp "$proc/environ" "$stage/application.env0"
  cp "$proc/cmdline" "$stage/application.argv0"
  cp "$proc/stat" "$stage/process.stat"
  [ -s "$stage/application.env0" ] && [ -s "$stage/application.argv0" ] || exit 1
  cp "$proc/environ" "$stage/check.env0"
  cp "$proc/cmdline" "$stage/check.argv0"
  cmp -s "$stage/application.env0" "$stage/check.env0"
  cmp -s "$stage/application.argv0" "$stage/check.argv0"
  rm "$stage/check.env0" "$stage/check.argv0"
  # /app/data is independent of the shared /books and /bookdrop stores.
  tar -C "${GRIMMORY_APP_DATA:-/app/data}" -cf "$stage/app-data.tar" .
}
case "$mode" in
  capture)
    # Invalidate before work, so failure cannot reuse a previously complete point.
    rm -f "$root/COMPLETE"
    copy_effective
    generation=$(cat "${GRIMMORY_PROC_ROOT:-/proc}/sys/kernel/random/uuid")
    printf '%s\n' "$generation" | LC_ALL=C grep -Eq '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
    printf 'format=grimmory-runtime-v1\ngeneration=%s\ncaptured_epoch=%s\n' "$generation" "$(date +%s)" >"$stage/runtime.txt"
    # stat contains counters that vary. Record only the Java process birth tick.
    sed 's/.*) //' "$stage/process.stat" | awk '{print $20}' >"$stage/process-start.txt"
    rm "$stage/process.stat"
    (cd "$stage" && sha256sum application.env0 application.argv0 app-data.tar process-start.txt runtime.txt >runtime.sha256)
    chmod 640 "$stage/"*
    chown -R 0:568 "$stage"
    chmod 750 "$stage"
    rm -rf -- "$root/previous"
    if [ -e "$root/current" ]; then mv "$root/current" "$root/previous"; fi
    mv "$stage" "$root/current"
    printf '%s\n' "$generation" >"$root/COMPLETE"
    chown 0:568 "$root/COMPLETE"
    chmod 640 "$root/COMPLETE"
    sync
    ;;
  verify)
    [ -f "$root/COMPLETE" ] && [ ! -L "$root/current" ] || exit 1
    sh "$0" --bounded validate "$root/current"
    copy_effective
    for name in application.env0 application.argv0 app-data.tar; do
      cmp -s "$stage/$name" "$root/current/$name"
    done
    sed 's/.*) //' "$stage/process.stat" | awk '{print $20}' >"$stage/process-start.txt"
    cmp -s "$stage/process-start.txt" "$root/current/process-start.txt"
    bundle=${GRIMMORY_DB_BUNDLE:-/database-capture}
    [ "$(cat "$bundle/COMPLETE")" = "$(cat "$root/COMPLETE")" ]
    cmp -s "$bundle/runtime/runtime.sha256" "$root/current/runtime.sha256"
    (cd "$bundle" && sha256sum -c grimmory.sql.sha256 >/dev/null)
    [ "$(sed -n 's/^runtime_generation=//p' "$bundle/manifest.txt")" = "$(cat "$root/COMPLETE")" ]
    [ "$(sha256sum "$bundle/runtime/runtime.sha256" | cut -d' ' -f1)" = "$(sed -n 's/^runtime_manifest_sha256=//p' "$bundle/manifest.txt")" ]
    [ "$(sha256sum "$bundle/grimmory.sql" | cut -d' ' -f1)" = "$(sed -n 's/^sql_sha256=//p' "$bundle/manifest.txt")" ]
    ;;
  *) exit 1 ;;
esac
printf 'Grimmory effective runtime escrow %s passed\n' "$mode"
