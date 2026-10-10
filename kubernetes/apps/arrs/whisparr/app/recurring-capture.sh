#!/bin/sh
# The Whisparr main and log databases share one bounded native-writer hold.
set -eu
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
token=$(tr -d '-' </proc/sys/kernel/random/uuid)
cleanup() {
  sh "$helper" release "$token"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
sh "$helper" acquire "$token"
timeout --kill-after=30 600 sh /kopiur/capture.sh
(cd /config/.kopiur-postgres/current && sha256sum -c SHA256SUMS >/dev/null)
sh "$helper" check 1
printf 'Whisparr recurring native paired generation complete\n'
