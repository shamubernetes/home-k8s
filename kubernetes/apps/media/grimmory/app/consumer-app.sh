#!/bin/sh
# Native Java output can contain secrets. Keep it only in disposable memory.
set -eu
exec >/private/application-launcher.log 2>&1
pid=
stop_app() {
  if [ -n "$pid" ]; then
    kill -TERM "$pid" 2>/dev/null || true
    for _attempt in $(seq 1 30); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    kill -KILL "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    pid=
  fi
}
trap stop_app EXIT
trap 'exit 1' TERM INT
for _attempt in $(seq 1 450); do
  [ ! -f /control/failed ] || exit 1
  if [ -f /control/database-ready ]; then break; fi
  sleep 2
done
[ -f /control/database-ready ]
for generation in 1 2; do
  sh /tools/boot.sh >/private/application.log 2>&1 &
  pid=$!
  printf '%s\n' "$generation" > /control/application-generation
  for _attempt in $(seq 1 450); do
    if [ -f /control/complete ]; then exit 0; fi
    kill -0 "$pid" 2>/dev/null || { touch /control/failed; exit 1; }
    if [ "$generation" = 1 ] && [ -f /control/restart-app ]; then
      stop_app
      rm /control/restart-app
      break
    fi
    sleep 2
  done
done
touch /control/failed
exit 1
