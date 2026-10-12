#!/bin/sh
# App-local Java process-group fence. No shared PID namespace or added capability.
set -eu
native_umask=$(umask)
umask 077
state=/runtime-escrow/fence
max_hold=1800
reject() { printf 'Grimmory native fence rejected\n' >&2; exit 1; }
number() { case "$1" in ''|0*|*[!0-9]*|??????????????*) return 1 ;; esac; }
monotonic() { awk '{print int($1*1000)}' /proc/uptime; }
secure() {
  [ -d "$state" ] && [ ! -L "$state" ] || return 1
  [ "$(stat -c '%u:%a' "$state")" = 0:700 ] || return 1
  for name in request active child pending lock; do
    [ ! -L "$state/$name" ] || return 1
    if [ -e "$state/$name" ]; then
      [ -f "$state/$name" ] && [ "$(stat -c '%u:%a' "$state/$name")" = 0:600 ] || return 1
    fi
  done
}
birth() { sed 's/.*) //' "/proc/$1/stat" | awk '{print $20}'; }
read_request() {
  secure && [ -f "$state/request" ] || return 1
  IFS=' ' read -r pod token deadline end epoch extra <"$state/request" || return 1
  [ -z "$extra" ] || return 1
  printf '%s\n' "$pod" | grep -Eq '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' || return 1
  printf '%s\n' "$token" | grep -Eq '^[0-9a-f]{32}$' || return 1
  number "$deadline" && number "$end" || return 1
  printf '%s\n' "$epoch" | grep -Eq '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' || return 1
  remaining=$((deadline - $(date +%s)))
  [ "$remaining" -gt 0 ] && [ "$remaining" -le "$max_hold" ] || return 1
  # A rescheduled/rebooted replacement must not compare another boot's uptime.
  # It holds startup to the original wall deadline but cannot acknowledge capture.
  if [ "$epoch" = "$(cat /proc/sys/kernel/random/boot_id)" ]; then
    mono_remaining=$((end - $(monotonic)))
    [ "$mono_remaining" -gt 0 ] && [ "$mono_remaining" -le "$((max_hold*1000))" ] || return 1
  else
    mono_remaining=$((remaining*1000))
  fi
}
read_child() {
  secure && [ -f "$state/child" ] || return 1
  IFS=' ' read -r pid started extra <"$state/child" || return 1
  number "$pid" && number "$started" && [ -z "$extra" ] || return 1
  [ "$(cat "/proc/$pid/comm")" = java ] && [ "$(birth "$pid")" = "$started" ] || return 1
  [ "$(awk '/^Uid:/{print $2":"$3":"$4":"$5}' "/proc/$pid/status")" = 568:568:568:568 ] || return 1
  # The native entrypoint must exec Java in the owned session and process group.
  [ "$(sed 's/.*) //' "/proc/$pid/stat" | awk '{print $3":"$4}')" = "$pid:$pid" ]
}
stopped_group() {
  read_child || return 1
  found=0
  for status in /proc/[0-9]*/stat; do
    [ -r "$status" ] || continue
    group=$(sed 's/.*) //' "$status" | awk '{print $3}') || return 1
    [ "$group" = "$pid" ] || continue
    native=${status%/stat}
    [ "$(awk '/^Uid:/{print $2":"$3":"$4":"$5}' "$native/status")" = 568:568:568:568 ] || return 1
    for thread in "$native"/task/[0-9]*/status; do
      [ -r "$thread" ] || return 1
      kind=$(awk '/^State:/{print $2}' "$thread") || return 1
      case "$kind" in T|Z) ;; *) return 1 ;; esac
    done
    found=$((found+1))
  done
  [ "$found" -gt 0 ]
}
check() {
  minimum=$1
  number "$minimum" && [ "$minimum" -le "$max_hold" ] || return 1
  read_request && [ "$pod" = "${KOPIUR_POD_UID:-}" ] && [ "$epoch" = "$(cat /proc/sys/kernel/random/boot_id)" ] || return 1
  [ "$remaining" -ge "$minimum" ] && [ "$mono_remaining" -ge "$((minimum*1000))" ] || return 1
  [ -f "$state/active" ] && cmp -s "$state/request" "$state/active" && stopped_group
}
signal_group() { su-exec 568:568 /bin/kill "-$1" "-$child"; }
child_alive() {
  [ -r "/proc/$child/stat" ] && [ "$(birth "$child")" = "$child_birth" ] || return 1
  kind=$(sed 's/.*) //' "/proc/$child/stat" | awk '{print $1}') || return 1
  [ "$kind" != Z ]
}
case ${1:-} in
  supervise)
    shift
    [ "$$" = 1 ] && [ "$(id -u)" = 0 ] && [ "$#" -gt 0 ] || reject
    [ ! -L /runtime-escrow ] && [ ! -L "$state" ] || reject
    mkdir -p "$state"
    chown 0:0 "$state"
    chmod 700 "$state"
    # PVC fsGroup parents legitimately carry SGID, chmod 700 preserves that bit.
    # Control state has its own root-only contract, clear inherited SGID here.
    chmod g-s "$state"
    secure || reject
    rm -f "$state/active" "$state/child" "$state/pending"
    # A replacement never starts a writer during a retained absolute hold.
    # Old Pod/process evidence cannot admit capture against the replacement.
    restart_end=$(($(monotonic)+max_hold*1000))
    while read_request && [ "$(monotonic)" -lt "$restart_end" ]; do sleep 0.25; done
    rm -f "$state/request"
    child=
    child_birth=
    fenced=0
    cleanup() {
      rm -f "$state/active" "$state/child"
      if [ -n "$child" ] && child_alive; then
        if [ "$fenced" = 1 ] || read_request; then
          # Never resume a fenced native writer during an admitted capture.
          signal_group KILL 2>/dev/null || true
        else
          signal_group TERM 2>/dev/null || true
          ticks=0
          while child_alive && [ "$ticks" -lt 10 ]; do sleep 1; ticks=$((ticks+1)); done
          signal_group KILL 2>/dev/null || true
        fi
        wait "$child" 2>/dev/null || true
      fi
    }
    trap cleanup EXIT
    trap 'exit 143' TERM HUP INT
    (umask "$native_umask"; exec setsid "$@") &
    child=$!
    child_birth=$(birth "$child")
    ticks=0
    until [ -r "/proc/$child/comm" ] && [ "$(cat "/proc/$child/comm")" = java ]; do
      child_alive && [ "$ticks" -lt 120 ] || reject
      sleep 1; ticks=$((ticks+1))
    done
    printf '%s %s\n' "$child" "$(birth "$child")" >"$state/pending"
    mv "$state/pending" "$state/child"
    read_child || reject
    while child_alive; do
      if read_request && [ "$pod" = "${KOPIUR_POD_UID:-}" ] && [ "$epoch" = "$(cat /proc/sys/kernel/random/boot_id)" ]; then
        accepted=$(cat "$state/request")
        fenced=1
        signal_group STOP || reject
        ticks=0
        until stopped_group; do
          read_request && [ "$ticks" -lt 40 ] || reject
          sleep 0.25; ticks=$((ticks+1))
        done
        read_request && [ "$(cat "$state/request")" = "$accepted" ] || reject
        printf '%s\n' "$accepted" >"$state/pending"
        mv "$state/pending" "$state/active"
        while read_request && [ "$(cat "$state/request")" = "$accepted" ]; do sleep 0.25; done
        rm -f "$state/active" "$state/request"
        signal_group CONT || reject
        fenced=0
      fi
      sleep 0.25
    done
    wait "$child"
    ;;
  acquire)
    [ "$#" -eq 3 ] && [ "$(id -u)" = 0 ] || reject
    secure || reject
    exec 8>"$state/lock"
    flock -n 8 || reject
    read_child && [ ! -e "$state/request" ] && [ ! -e "$state/active" ] || reject
    deadline=$2; minimum=$3
    number "$deadline" || reject
    number "$minimum" || reject
    remaining=$((deadline - $(date +%s)))
    [ "$remaining" -ge "$minimum" ] && [ "$remaining" -le "$max_hold" ] || reject
    token=$(tr -d '-' </proc/sys/kernel/random/uuid)
    end=$(($(monotonic)+remaining*1000))
    pod=${KOPIUR_POD_UID:?missing Pod identity}
    epoch=$(cat /proc/sys/kernel/random/boot_id)
    (set -C; printf '%s %s %s %s %s\n' "$pod" "$token" "$deadline" "$end" "$epoch" >"$state/pending")
    mv "$state/pending" "$state/request"
    ticks=0
    until check "$minimum"; do
      read_request && [ "$ticks" -lt 80 ] || { rm -f "$state/request"; reject; }
      sleep 0.25; ticks=$((ticks+1))
    done
    cat "$state/active"
    ;;
  check)
    [ "$#" -eq 3 ] && check "$3" && [ "$(cat "$state/active")" = "$2" ] || reject
    ;;
  status)
    [ "$#" -eq 1 ] || reject
    check 1 || reject
    cat "$state/active"
    ;;
  process)
    [ "$#" -eq 1 ] || reject
    read_child || reject
    printf '%s\n' "$pid"
    ;;
  release)
    [ "$#" -eq 2 ] || reject
    secure || reject
    if [ -f "$state/request" ] && [ "$(cat "$state/request")" = "$2" ]; then rm -f "$state/request"; fi
    ;;
  liveness)
    check 1 || exec wget -q -T 2 -O /dev/null http://127.0.0.1:6060/api/v1/healthcheck
    ;;
  *) reject ;;
esac
