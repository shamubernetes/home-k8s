#!/bin/sh
# Keep the existing PodTemplate entrypoint hot-load compatible. Capture never
# signals the writer. Legacy acquire/check/release requests fail closed.
set -eu
umask 077
[ "$#" -gt 0 ] || exit 2
case "$1" in
  supervise)
    shift
    [ "$#" -gt 0 ]
    exec "$@"
    ;;
  liveness)
    exec node /kopiur/capture.cjs liveness
    ;;
  *) exit 2 ;;
esac
