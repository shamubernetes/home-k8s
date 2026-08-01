#!/bin/sh
set -eu

# Gatus only notices configuration changes when a remaining file has a newer
# modification time. A ConfigMap deletion removes its fragment, so write an
# atomic, harmless fragment to make deletions trigger the built-in reload.
# Flux resolves the escaped references below before ShellCheck sees their uses.
# shellcheck disable=SC2034
temporary=$(mktemp /config/.zz-sidecar-reload.XXXXXX)
printf '%s\n' 'endpoints: []' > "$${temporary}"
mv "$${temporary}" /config/zz-sidecar-reload.yaml
