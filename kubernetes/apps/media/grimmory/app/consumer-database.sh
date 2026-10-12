#!/bin/bash
# Disposable native database only. All output stays in the Pod's memory volume.
set -Eeuo pipefail
exec >/private/database-proof.log 2>&1
trap 'touch /control/failed' ERR
set -a
# Generated at runtime inside the owned Pod's memory volume.
# shellcheck disable=SC1091
source /tools/database.env
set +a
export MYSQL_PWD="$MYSQL_PASSWORD"
client=(mariadb --protocol=tcp --host=127.0.0.1 --port=3306 --user="$MYSQL_USER" --connect-timeout=5 --batch --skip-column-names)
ready=false
for ((attempt=0; attempt<150; attempt++)); do
  if "${client[@]}" -e 'SELECT 1' >/dev/null 2>&1; then ready=true; break; fi
  sleep 2
done
[[ $ready == true ]]
K8S92_ISOLATED_RESTORE=YES bash /contract/restore.sh /original/mariadb-config/kopiur
expected=57d2e92411667e41fedfee17fcd0824a5b938becb06750b2a45ddcf7f107a782
verify_data() {
  timeout --signal=TERM --kill-after=5s 60s mariadb-dump --protocol=tcp -h127.0.0.1 -u"$MYSQL_USER" \
    --skip-comments --compact --no-create-info --skip-add-locks --skip-disable-keys \
    --skip-extended-insert --order-by-primary --hex-blob grimmory > /private/data.sql
  actual=$(sha256sum /private/data.sql | cut -d' ' -f1)
  rm /private/data.sql
  [[ $actual == "$expected" ]]
}
verify_data
[[ $("${client[@]}" grimmory -e 'SELECT COUNT(*) FROM users') == 0 ]]
printf '%s\n' "$expected" > /control/database-ready
for ((attempt=0; attempt<450; attempt++)); do
  if [[ -f /control/check-data ]]; then
    verify_data
    mv /control/check-data /control/data-equal
  fi
  if [[ -f /control/complete ]]; then exit 0; fi
  sleep 2
done
touch /control/failed
exit 1
