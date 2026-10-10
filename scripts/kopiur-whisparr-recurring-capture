#!/bin/sh
# The Whisparr main and log databases share one bounded native-writer hold.
set -eu

# Policy-set sorts ignore rules. Keep native whitelist order in .kopiaignore.
# The paired archive contains the complete stopped-writer application tree.
configure_bundle_ignore() {
  config=$1
  mode=$2
  case "$mode" in install|remove) ;; *) return 1 ;; esac
  [ -d "$config" ] && [ ! -L "$config" ] || return 1
  ignore="$config/.kopiaignore"
  ignore_tmp=$(mktemp "$config/.kopiur-ignore.XXXXXX")
  printf '%s\n' '# Kopiur Whisparr paired native recovery bundle.' \
    '/*' '!/.kopiur-postgres' '/.kopiur-postgres/*' \
    '!/.kopiur-postgres/COMPLETE' '!/.kopiur-postgres/current' >"$ignore_tmp"
  result=0
  if [ -e "$ignore" ] || [ -L "$ignore" ]; then
    # Never replace an unknown user file, symlink, hardlink or special file.
    if [ -f "$ignore" ] && [ ! -L "$ignore" ] && \
      [ "$(stat -c %h "$ignore")" = 1 ] && cmp -s "$ignore_tmp" "$ignore"; then
      if [ "$mode" = remove ]; then rm "$ignore" || result=1; fi
    else
      result=1
    fi
  elif [ "$mode" = install ]; then
    ln "$ignore_tmp" "$ignore" || result=1
  fi
  rm -f "$ignore_tmp"
  return "$result"
}

state=$(printenv QUIESCENCE_STATE_DIR || printf /kopiur-quiescence)
helper=$(printenv QUIESCENCE_HELPER || printf /kopiur/recurring.sh)
[ "$(printenv CAPTURE_MODE || true)" = quiesced-whisparr-stable-filetree ] || exit 1
[ "$(printenv PGDATABASES || true)" = 'whisparrv3_main whisparrv3_logs' ] || exit 1
[ -d "$state" ] && [ ! -L "$state" ] || exit 1
[ ! -L "$state/client-lock" ] || exit 1
if [ -e "$state/client-lock" ]; then
  [ -f "$state/client-lock" ] && [ "$(stat -c %h "$state/client-lock")" = 1 ] || exit 1
fi
exec 8>>"$state/client-lock"
flock -n 8 || exit 1
# Include the owned filter in the paired archive before acquiring the writer hold.
# Rollback first sets false in GitOps, captures once and verifies filter removal.
case "$(printenv KOPIUR_BUNDLE_ONLY || printf true)" in
  true) configure_bundle_ignore /config install ;;
  false) configure_bundle_ignore /config remove ;;
  *) exit 1 ;;
esac
token=$(tr -d '-' </proc/sys/kernel/random/uuid)
cleanup() {
  sh "$helper" release "$token"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
sh "$helper" acquire "$token"
# The 12.96GB production archive succeeded once but a second serial capture
# hit the 900s subprocess bound. Keep the qualified 1800s absolute watchdog.
# Require 1500s for 1200s capture plus hashing/escalation inside that hold.
sh "$helper" check 1500
timeout --kill-after=30 1200 sh /kopiur/capture.sh
(cd /config/.kopiur-postgres/current && sha256sum -c SHA256SUMS >/dev/null)
sh "$helper" check 1
printf 'Whisparr recurring native paired generation complete\n'
