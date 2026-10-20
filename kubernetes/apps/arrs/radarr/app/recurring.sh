#!/bin/sh
# Compatibility for existing PodTemplates. Backup hooks never own app lifecycle.
set -eu
case "$1" in
  supervise)
    shift
    [ "$#" -gt 0 ]
    exec "$@"
    ;;
  liveness) exec curl -fsS --max-time 2 http://127.0.0.1:80/ping ;;
  release) exit 0 ;;
  # Fail closed for stale callers instead of authoring any writer-hold marker.
  *) exit 2 ;;
esac
