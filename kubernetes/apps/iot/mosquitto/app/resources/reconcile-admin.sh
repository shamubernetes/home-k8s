#!/bin/sh
set -eu

: "${MOSQUITTO_ADMIN_USERNAME:?MOSQUITTO_ADMIN_USERNAME is required}"
: "${MOSQUITTO_ADMIN_PASSWORD:?MOSQUITTO_ADMIN_PASSWORD is required}"

config=/mosquitto/data/dynamic-security.json
if [ ! -s "$config" ]; then
  echo "Dynamic Security configuration is missing" >&2
  exit 1
fi

# The retained PVC is authoritative for clients, roles, and ACLs. Reconcile only
# the administrator password from 1Password so rotations do not require deleting
# that state or recreating Zoo Fleet identities.
mosquitto_ctrl -f "$config" dynsec setClientPassword \
  "$MOSQUITTO_ADMIN_USERNAME" "$MOSQUITTO_ADMIN_PASSWORD"
chmod 0600 "$config"
echo "Dynamic Security administrator credential reconciled"
