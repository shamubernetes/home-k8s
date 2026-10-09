#!/bin/sh
# Radarr owns its child process group. The exporter only requests a bounded hold.
set -eu
umask 077
state=$(printenv QUIESCENCE_STATE_DIR || printf /kopiur-quiescence)
max_hold=1800
valid_number() {
  case "$1" in ''|0*|*[!0-9]*|???????????*) return 1 ;; esac
}
regular() {
  [ ! -L "$1" ] && { [ ! -e "$1" ] || [ -f "$1" ]; }
}
valid_state() {
  [ -d "$state" ] && [ ! -L "$state" ] || return 1
  for file in boot request active pending client-pending capture-owner capture-owner-pending; do
    regular "$state/$file" || return 1
  done
}
read_request() {
  valid_state || return 1
  [ -f "$state/boot" ] && [ -f "$state/request" ] || return 1
  IFS=' ' read -r generation token deadline extra <"$state/request" || return 1
  [ -z "$extra" ] && [ "$generation" = "$(cat "$state/boot")" ] || return 1
  case "$token" in ''|*[!a-f0-9]*|?????????????????????????????????*) return 1 ;; esac
  valid_number "$deadline" || return 1
  remaining=$((deadline - $(date +%s)))
  [ "$remaining" -gt 0 ] && [ "$remaining" -le "$max_hold" ]
}
check_hold() {
  minimum=$1
  valid_number "$minimum" || return 1
  read_request || return 1
  [ "$remaining" -ge "$minimum" ] && [ -f "$state/active" ] || return 1
  cmp -s "$state/request" "$state/active"
}
case "$1" in
  supervise)
    shift
    [ "$#" -gt 0 ]
    mkdir -p "$state"
    valid_state || exit 1
    # A Pod/container replacement must preserve an accepted capture hold. Keep the
    # original absolute expiry, never extend it or start a new native writer.
    rm -f "$state/active"
    if read_request; then
      accepted=$(cat "$state/request")
      printf '%s\n' "$accepted" >"$state/pending"
      mv "$state/pending" "$state/active"
      ticks=0
      while read_request && [ "$(cat "$state/request")" = "$accepted" ] && [ "$ticks" -lt "$max_hold" ]; do
        sleep 1
        ticks=$((ticks + 1))
      done
    fi
    rm -f "$state/active" "$state/request" "$state/pending" "$state/client-pending"
    tr -d '-' </proc/sys/kernel/random/uuid >"$state/pending"
    mv "$state/pending" "$state/boot"
    child=
    cleanup() {
      # Keep boot/request so a replacement preserves the same absolute hold.
      rm -f "$state/active"
      if [ -n "$child" ]; then
        /bin/kill -TERM -- "-$child" 2>/dev/null || true
        wait "$child" 2>/dev/null || true
      fi
    }
    trap cleanup EXIT
    trap 'exit 143' HUP INT TERM
    start_child() {
      setsid "$@" &
      child=$!
    }
    stop_child() {
      /bin/kill -TERM -- "-$child" 2>/dev/null || true
      ticks=0
      while kill -0 "$child" 2>/dev/null && [ "$ticks" -lt 60 ]; do
        sleep 1
        ticks=$((ticks + 1))
      done
      /bin/kill -KILL -- "-$child" 2>/dev/null || true
      wait "$child" 2>/dev/null || true
      # Reject residual descendants. Never acknowledge a live writer.
      if /bin/kill -0 -- "-$child" 2>/dev/null; then exit 1; fi
      child=
    }
    start_child "$@"
    while :; do
      if ! kill -0 "$child" 2>/dev/null; then
        wait "$child" || exit $?
        exit 1
      fi
      if read_request; then
        accepted=$(cat "$state/request")
        stop_child
        read_request && [ "$(cat "$state/request")" = "$accepted" ] || {
          start_child "$@"
          continue
        }
        printf '%s\n' "$accepted" >"$state/pending"
        mv "$state/pending" "$state/active"
        ticks=0
        # Both absolute expiry and elapsed cap prevent indefinite downtime.
        while read_request && [ "$(cat "$state/request")" = "$accepted" ] && [ "$ticks" -lt "$max_hold" ]; do
          sleep 1
          ticks=$((ticks + 1))
        done
        rm -f "$state/active" "$state/request"
        start_child "$@"
      fi
      sleep 1
    done
    ;;
  acquire)
    valid_state || exit 1
    [ -f "$state/boot" ] && [ ! -e "$state/request" ] && [ ! -e "$state/active" ] || exit 1
    generation=$(cat "$state/boot")
    token=$(tr -d '-' </proc/sys/kernel/random/uuid)
    [ "$#" -lt 2 ] || token=$2
    case "$token" in ''|*[!a-f0-9]*|?????????????????????????????????*) exit 1 ;; esac
    hold=$max_hold
    minimum=1320
    [ "$#" -lt 3 ] || hold=$3
    [ "$#" -lt 4 ] || minimum=$4
    valid_number "$hold" && valid_number "$minimum" || exit 1
    [ "$hold" -le "$max_hold" ] && [ "$minimum" -le "$hold" ] || exit 1
    deadline=$(($(date +%s) + hold))
    printf '%s %s %s\n' "$generation" "$token" "$deadline" >"$state/capture-owner-pending"
    mv "$state/capture-owner-pending" "$state/capture-owner"
    cp "$state/capture-owner" "$state/client-pending"
    mv "$state/client-pending" "$state/request"
    expected="$generation $token $deadline"
    ticks=0
    while [ "$ticks" -lt 120 ]; do
      check_hold "$minimum" && [ "$(cat "$state/active")" = "$expected" ] && exit 0
      sleep 1
      ticks=$((ticks + 1))
    done
    if cmp -s "$state/request" "$state/capture-owner"; then rm -f "$state/request"; fi
    rm -f "$state/capture-owner"
    exit 1
    ;;
  check)
    check_hold "$2"
    ;;
  release)
    valid_state || exit 1
    if [ -f "$state/capture-owner" ]; then
      IFS=' ' read -r owner_generation owner_token owner_deadline extra <"$state/capture-owner" || exit 1
      [ -z "$extra" ] && valid_number "$owner_deadline" || exit 1
      [ "$owner_generation" = "$(cat "$state/boot")" ] || exit 0
      [ "$#" -lt 2 ] || [ "$owner_token" = "$2" ] || exit 0
      if cmp -s "$state/request" "$state/capture-owner"; then rm -f "$state/request"; fi
      rm -f "$state/capture-owner"
    fi
    ;;
  liveness)
    # Real readiness remains HTTP-only. Only a proved bounded stop suppresses
    # liveness restarts while the native child is intentionally absent.
    check_hold 1 || exec curl -fsS --max-time 2 http://127.0.0.1:80/ping
    ;;
  *) exit 2 ;;
esac
