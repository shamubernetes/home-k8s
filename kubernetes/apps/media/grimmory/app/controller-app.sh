#!/bin/sh
# Native output and captured configuration stay only in disposable Pod memory.
set -eu
exec >/private/application-controller.log 2>&1
pid=
# Called by the EXIT trap, including kubelet TERM after the native probe fault.
# shellcheck disable=SC2329
stop_app() {
  if [ -n "$pid" ]; then
    kill -CONT "$pid" 2>/dev/null || true
    kill -TERM "$pid" 2>/dev/null || true
    for _attempt in $(seq 1 30); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    kill -KILL "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
}
trap 'stop_app' EXIT
trap 'exit 0' TERM INT
for _attempt in $(seq 1 300); do
  [ ! -f /control/failed ] || exit 1
  if [ -f /control/database-ready ]; then break; fi
  sleep 2
done
[ -f /control/database-ready ]
sh /tools/boot.sh >/private/application.log 2>&1 &
pid=$!
cat /proc/sys/kernel/random/uuid > /control/application-epoch
while kill -0 "$pid" 2>/dev/null; do
  if [ -f /control/freeze-application ]; then
    rm /control/freeze-application
    kill -STOP "$pid"
    touch /control/application-frozen
    # Leave the native process stopped. Only the incumbent kubelet HTTP
    # liveness probe may recover this fault; the wrapper does not restart it.
  fi
  sleep 2
done
touch /control/failed
exit 1
