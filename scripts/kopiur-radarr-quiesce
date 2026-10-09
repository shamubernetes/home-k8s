#!/bin/sh
# One Radarr maintenance pause. The reviewed manifest supplies an absolute expiry.
set -eu
umask 077
state=$(printenv QUIESCENCE_STATE_DIR || printf /kopiur-quiescence)
mode=$1
deadline=$2
shift 2
max_hold=1800
valid_number() {
  case "$1" in ''|0*|*[!0-9]*|???????????*) return 1 ;; esac
}
valid_state() {
  [ -d "$state" ] && [ ! -L "$state" ] &&
    [ ! -L "$state/active" ] && [ ! -L "$state/pending" ] &&
    { [ ! -e "$state/active" ] || [ -f "$state/active" ]; } &&
    { [ ! -e "$state/pending" ] || [ -f "$state/pending" ]; }
}
case "$mode" in
  start)
    [ "$#" -gt 0 ]
    # A restarted wrapper cannot leave old pause evidence valid on fail-open.
    if [ -e "$state" ] || [ -L "$state" ]; then
      if ! valid_state; then
        # Non-regular evidence already fails the guard. A directory must not
        # prevent restoring service, and removing a link never follows its target.
        if [ -d "$state" ] && [ ! -L "$state" ] && [ ! -d "$state/active" ]; then
          rm -f "$state/active"
        fi
        exec "$@"
      fi
      rm -f "$state/active"
    fi
    # Bad/expired input must restore service, never create an indefinite pause.
    if ! valid_number "$deadline"; then exec "$@"; fi
    now=$(date +%s)
    if [ "$deadline" -le "$now" ] || [ "$((deadline - now))" -gt "$max_hold" ]; then
      exec "$@"
    fi
    if [ -L "$state" ]; then exec "$@"; fi
    mkdir -p "$state"
    if ! valid_state; then exec "$@"; fi
    printf '%s\n' "$deadline" >"$state/pending"
    mv "$state/pending" "$state/active"
    trap 'rm -f "$state/active"' EXIT
    trap 'exit 143' HUP INT TERM
    printf 'Radarr startup paused for bounded Kopiur capture\n'
    ticks=0
    while [ "$(date +%s)" -lt "$deadline" ] && [ "$ticks" -lt "$max_hold" ]; do
      sleep 1
      ticks=$((ticks + 1))
    done
    rm -f "$state/active"
    trap - EXIT HUP INT TERM
    exec "$@"
    ;;
  check)
    [ "$#" -eq 1 ]
    minimum=$1
    valid_number "$deadline" || exit 1
    valid_number "$minimum" || exit 1
    [ "$minimum" -gt 0 ] && [ "$minimum" -le "$max_hold" ]
    valid_state || exit 1
    [ -f "$state/active" ] || exit 1
    [ "$(cat "$state/active")" = "$deadline" ]
    remaining=$((deadline - $(date +%s)))
    [ "$remaining" -ge "$minimum" ] && [ "$remaining" -le "$max_hold" ]
    ;;
  *) exit 2 ;;
esac
