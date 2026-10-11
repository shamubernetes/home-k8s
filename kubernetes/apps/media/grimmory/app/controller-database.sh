#!/bin/bash
# Only the disposable database receives this fault. Logs remain in Pod memory.
set -Eeuo pipefail
exec >/private/database-controller.log 2>&1
trap 'touch /control/failed' ERR
set -a
# shellcheck disable=SC1091
source /tools/database.env
set +a
export MYSQL_PWD="$MYSQL_PASSWORD"
client=(mariadb --protocol=tcp --host=127.0.0.1 --port=3306 --user="$MYSQL_USER" --connect-timeout=5 --batch --skip-column-names)
rm -f /control/database-ready
ready=false
for ((attempt=0; attempt<150; attempt++)); do
  if "${client[@]}" -e 'SELECT 1' >/dev/null 2>&1; then ready=true; break; fi
  sleep 2
done
[[ $ready == true ]]
if [[ ! -f /control/database-initialized ]]; then
  K8S92_ISOLATED_RESTORE=YES bash /contract/restore.sh /original/mariadb-config/kopiur
fi
expected=57d2e92411667e41fedfee17fcd0824a5b938becb06750b2a45ddcf7f107a782
verify_data() {
  timeout --signal=TERM --kill-after=5s 60s mariadb-dump --protocol=tcp -h127.0.0.1 -u"$MYSQL_USER" \
    --skip-comments --compact --no-create-info --skip-add-locks --skip-disable-keys \
    --skip-extended-insert --order-by-primary --hex-blob grimmory > /private/controller-data.sql
  actual=$(sha256sum /private/controller-data.sql | cut -d' ' -f1)
  rm /private/controller-data.sql
  [[ $actual == "$expected" ]]
}
verify_data
touch /control/database-initialized
cat /proc/sys/kernel/random/uuid > /control/database-epoch
printf '%s\n' "$expected" > /control/database-ready
while true; do
  if [[ -f /control/check-data ]]; then
    verify_data
    mv /control/check-data /control/data-equal
  fi
  if [[ -f /control/replace-database ]]; then
    rm /control/replace-database /control/database-ready
    # Freeze only the native daemon as its incumbent owner. s6 PID 1 can
    # consume TERM without exiting; its ping probe must recover this fault.
    # Variables belong to the UID-568 child shell, not the root monitor.
    # shellcheck disable=SC2016
    s6-setuidgid abc bash -c '
      set -euo pipefail
      targets=()
      for file in /proc/[0-9]*/comm; do
        IFS= read -r comm <"$file" || continue
        if [[ $comm == mariadbd ]]; then
          pid=${file%/comm}
          targets+=("${pid##*/}")
        fi
      done
      [[ ${#targets[@]} == 1 && ${targets[0]} -gt 1 ]]
      kill -STOP "${targets[0]}"
    '
    exit 0
  fi
  sleep 2
done
