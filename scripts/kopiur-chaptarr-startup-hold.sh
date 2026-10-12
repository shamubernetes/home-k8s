#!/bin/bash
# UID568 startup exclusion before the exact pinned Chaptarr native entrypoint.
# A deadline of zero stages the contract without pausing the application.
set -euo pipefail
STATE=/tmp/kopiur-books-hold
MAX_HOLD=1800
reject() { printf 'Chaptarr startup hold rejected\n' >&2; exit 1; }
monotonic() { awk '{print int($1*1000)}' /proc/uptime; }
secure() {
  [[ -d "$STATE" && ! -L "$STATE" ]] || reject
  [[ $(stat -c '%u:%a' "$STATE") == 568:700 ]] || reject
  if [[ -e "$STATE/active" || -L "$STATE/active" ]]; then
    [[ -f "$STATE/active" && ! -L "$STATE/active" ]] || reject
    [[ $(stat -c '%u:%a' "$STATE/active") == 568:600 ]] || reject
  fi
}
identity() {
  [[ ${KOPIUR_POD_UID:-} =~ ^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$ ]] || reject
  local proc rest command
  proc=$(cat /proc/1/stat)
  rest=${proc##*) }
  read -r -a fields <<< "$rest"
  command=$(sha256sum /proc/1/cmdline)
  printf '%s\n%s\n%s\n%s\n' "$KOPIUR_POD_UID" "$(cat /proc/sys/kernel/random/boot_id)" "${fields[19]}" "${command%% *}"
}
remove_marker() {
  secure
  rm -f -- "$STATE/active"
}
check() {
  local minimum=${1:-1} now mono current
  [[ $minimum =~ ^[1-9][0-9]*$ && ${#minimum} -le 4 && $minimum -le $MAX_HOLD ]] || reject
  secure
  [[ -f "$STATE/active" ]] || reject
  mapfile -t marker < "$STATE/active"
  [[ ${#marker[@]} -eq 7 ]] || reject
  current=$(identity)
  [[ $(printf '%s\n' "${marker[@]:0:4}") == "$current" ]] || reject
  [[ ${marker[4]} =~ ^[0-9a-f]{64}$ ]] || reject
  [[ ${marker[5]} =~ ^[1-9][0-9]{0,9}$ && ${marker[6]} =~ ^[1-9][0-9]{0,12}$ ]] || reject
  now=$(date +%s)
  mono=$(monotonic)
  [[ $((marker[5]-now)) -ge $minimum && $((marker[5]-now)) -le $MAX_HOLD ]] || reject
  [[ $((marker[6]-mono)) -ge $((minimum*1000)) && $((marker[6]-mono)) -le $((MAX_HOLD*1000)) ]] || reject
  if [[ $# -ge 2 ]]; then
    [[ $(cat "$STATE/active") == "$2" ]] || reject
  else
    cat "$STATE/active"
  fi
}
start() {
  [[ $$ -eq 1 && $(id -u) == 568 && $(id -g) == 568 ]] || reject
  local deadline=$1 now duration end nonce
  [[ $deadline =~ ^[0-9]{1,10}$ ]] || reject
  if [[ -e "$STATE" || -L "$STATE" ]]; then remove_marker; fi
  now=$(date +%s)
  duration=$((10#$deadline-now))
  if (( duration <= 0 || duration > MAX_HOLD )); then
    exec /entrypoint.sh dotnet /app/Chaptarr.dll -nobrowser -data=/config
  fi
  local original_umask
  original_umask=$(umask)
  umask 077
  mkdir -p -- "$STATE"
  secure
  end=$(($(monotonic)+duration*1000))
  nonce=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
  [[ $nonce =~ ^[0-9a-f]{64}$ ]] || reject
  # The marker is never sourced or evaluated. Refuse pre-existing pending files.
  (set -o noclobber; { identity; printf '%s\n%s\n%s\n' "$nonce" "$deadline" "$end"; } > "$STATE/pending-$nonce")
  mv -- "$STATE/pending-$nonce" "$STATE/active"
  trap 'remove_marker; exit 143' TERM
  trap 'remove_marker; exit 130' INT
  trap 'remove_marker; exit 129' HUP
  while (( $(date +%s) < deadline && $(monotonic) < end )); do sleep 0.25; done
  remove_marker
  # Restore the original application umask before the native entrypoint.
  umask "$original_umask"
  exec /entrypoint.sh dotnet /app/Chaptarr.dll -nobrowser -data=/config
}
case ${1:-} in
  start) [[ $# -eq 2 ]] || reject; start "$2" ;;
  status) [[ $# -eq 1 ]] || reject; check ;;
  check) [[ $# -eq 3 ]] || reject; check "$3" "$2" ;;
  *) reject ;;
esac
