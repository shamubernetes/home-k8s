#!/bin/sh
set -eu

# Gatus only notices configuration changes when a remaining file has a newer
# modification time. A ConfigMap deletion removes its fragment, so write an
# atomic, harmless fragment to make deletions trigger the built-in reload.
temporary=$(mktemp /config/.zz-sidecar-reload.XXXXXX)
printf '%s\n' 'endpoints: []' > "$${temporary}"
mv "$${temporary}" /config/zz-sidecar-reload.yaml
