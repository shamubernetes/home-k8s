#!/bin/sh
set -eu

: "${MQTT_HOST:?MQTT_HOST is required}"
: "${MQTT_ADMIN_USERNAME:?MQTT_ADMIN_USERNAME is required}"
: "${MQTT_ADMIN_PASSWORD:?MQTT_ADMIN_PASSWORD is required}"
: "${MQTT_HA_PASSWORD:?MQTT_HA_PASSWORD is required}"
: "${MQTT_ESPRESENSE_BAR_PASSWORD:?MQTT_ESPRESENSE_BAR_PASSWORD is required}"
: "${MQTT_ESPRESENSE_LIVINGROOM_PASSWORD:?MQTT_ESPRESENSE_LIVINGROOM_PASSWORD is required}"
: "${MQTT_RATGDO_MAIN_PASSWORD:?MQTT_RATGDO_MAIN_PASSWORD is required}"
: "${MQTT_RATGDO_LEGACY_PASSWORD:?MQTT_RATGDO_LEGACY_PASSWORD is required}"

MQTT_PORT=8883
ADMIN_OPTIONS=/work/admin-options

printf '%s\n' \
  '--tls-use-os-certs' \
  "-h ${MQTT_HOST}" \
  "-p ${MQTT_PORT}" \
  "-u ${MQTT_ADMIN_USERNAME}" \
  "-P ${MQTT_ADMIN_PASSWORD}" > "$ADMIN_OPTIONS"
chmod 0600 "$ADMIN_OPTIONS"

ctrl() {
  mosquitto_ctrl -o "$ADMIN_OPTIONS" dynsec "$@"
}

attempt=0
until ctrl listClients >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "Broker did not become ready within five minutes" >&2
    exit 1
  fi
  sleep 5
done

ensure_client() {
  username="$1"
  password="$2"
  if ctrl listClients | grep -Fxq "$username"; then
    printf '%s\n%s\n' "$password" "$password" |
      ctrl setClientPassword "$username" >/dev/null
  else
    printf '%s\n%s\n' "$password" "$password" |
      ctrl createClient "$username" >/dev/null
  fi
}

ensure_role() {
  role="$1"
  shift
  if ctrl listRoles | grep -Fxq "$role"; then
    ctrl deleteRole "$role" >/dev/null
  fi
  ctrl createRole "$role" >/dev/null
  while [ "$#" -gt 0 ]; do
    acltype="$1"
    topic="$2"
    shift 2
    ctrl addRoleACL "$role" "$acltype" "$topic" allow 100 >/dev/null
  done
}

ensure_assignment() {
  username="$1"
  role="$2"
  if ! ctrl getClient "$username" | grep -Fq "$role"; then
    ctrl addClientRole "$username" "$role" 100 >/dev/null
  fi
}

remove_obsolete_managed_roles() {
  for role in $(ctrl listRoles | grep '^ha-migration-' || true); do
    case "$role" in
      "$HA_ROLE"|"$BAR_ROLE"|"$LIVINGROOM_ROLE"|"$RATGDO_ROLE"|"$LEGACY_RATGDO_ROLE") ;;
      *) ctrl deleteRole "$role" >/dev/null ;;
    esac
  done
}

HA_USER=home-assistant
BAR_USER=espresense-bar
LIVINGROOM_USER=espresense-livingroom
RATGDO_USER=ratgdo-main-garage
LEGACY_RATGDO_USER=ratgdo
HA_ROLE=ha-migration-home-assistant-v1
BAR_ROLE=ha-migration-espresense-bar-v1
LIVINGROOM_ROLE=ha-migration-espresense-livingroom-v1
RATGDO_ROLE=ha-migration-ratgdo-main-v1
LEGACY_RATGDO_ROLE=ha-migration-ratgdo-legacy-v1

ensure_client "$HA_USER" "$MQTT_HA_PASSWORD"
ensure_client "$BAR_USER" "$MQTT_ESPRESENSE_BAR_PASSWORD"
ensure_client "$LIVINGROOM_USER" "$MQTT_ESPRESENSE_LIVINGROOM_PASSWORD"
ensure_client "$RATGDO_USER" "$MQTT_RATGDO_MAIN_PASSWORD"
ensure_client "$LEGACY_RATGDO_USER" "$MQTT_RATGDO_LEGACY_PASSWORD"

ensure_role "$HA_ROLE" \
  subscribePattern 'homeassistant/#' \
  unsubscribePattern 'homeassistant/#' \
  publishClientReceive 'homeassistant/+/espresense_5c1fa8/#' \
  publishClientReceive 'homeassistant/+/espresense_9810a8/#' \
  publishClientReceive 'homeassistant/+/Main_Garage_Door/#' \
  publishClientReceive 'homeassistant/+/Small_Garage_Door/#' \
  subscribePattern 'espresense/rooms/bar/#' \
  unsubscribePattern 'espresense/rooms/bar/#' \
  publishClientReceive 'espresense/rooms/bar/#' \
  subscribePattern 'espresense/rooms/livingroom/#' \
  unsubscribePattern 'espresense/rooms/livingroom/#' \
  publishClientReceive 'espresense/rooms/livingroom/#' \
  subscribePattern 'espresense/devices/+/bar' \
  unsubscribePattern 'espresense/devices/+/bar' \
  publishClientReceive 'espresense/devices/+/bar' \
  subscribePattern 'espresense/devices/+/livingroom' \
  unsubscribePattern 'espresense/devices/+/livingroom' \
  publishClientReceive 'espresense/devices/+/livingroom' \
  subscribePattern 'ratgdo_Main_Garage_Door/status/#' \
  unsubscribePattern 'ratgdo_Main_Garage_Door/status/#' \
  publishClientReceive 'ratgdo_Main_Garage_Door/status/#' \
  subscribePattern 'ratgdo_Small_Garage_Door/status/#' \
  unsubscribePattern 'ratgdo_Small_Garage_Door/status/#' \
  publishClientReceive 'ratgdo_Small_Garage_Door/status/#' \
  publishClientSend 'homeassistant/status' \
  publishClientSend 'espresense/settings/+/config' \
  publishClientSend 'espresense/rooms/*/+/set' \
  publishClientSend 'espresense/rooms/bar/#' \
  publishClientSend 'espresense/rooms/livingroom/#' \
  publishClientSend 'ratgdo_Main_Garage_Door/command/#' \
  publishClientSend 'ratgdo_Small_Garage_Door/command/#'

ensure_role "$BAR_ROLE" \
  publishClientSend 'homeassistant/+/espresense_5c1fa8/#' \
  publishClientSend 'espresense/rooms/bar/#' \
  publishClientSend 'espresense/devices/+/bar' \
  subscribePattern 'homeassistant/status' \
  unsubscribePattern 'homeassistant/status' \
  publishClientReceive 'homeassistant/status' \
  subscribePattern 'espresense/settings/+/config' \
  unsubscribePattern 'espresense/settings/+/config' \
  publishClientReceive 'espresense/settings/+/config' \
  subscribePattern 'espresense/rooms/*/+/set' \
  unsubscribePattern 'espresense/rooms/*/+/set' \
  publishClientReceive 'espresense/rooms/*/+/set' \
  subscribePattern 'espresense/rooms/bar/#' \
  unsubscribePattern 'espresense/rooms/bar/#' \
  publishClientReceive 'espresense/rooms/bar/#'

ensure_role "$LIVINGROOM_ROLE" \
  publishClientSend 'homeassistant/+/espresense_9810a8/#' \
  publishClientSend 'espresense/rooms/livingroom/#' \
  publishClientSend 'espresense/devices/+/livingroom' \
  subscribePattern 'homeassistant/status' \
  unsubscribePattern 'homeassistant/status' \
  publishClientReceive 'homeassistant/status' \
  subscribePattern 'espresense/settings/+/config' \
  unsubscribePattern 'espresense/settings/+/config' \
  publishClientReceive 'espresense/settings/+/config' \
  subscribePattern 'espresense/rooms/*/+/set' \
  unsubscribePattern 'espresense/rooms/*/+/set' \
  publishClientReceive 'espresense/rooms/*/+/set' \
  subscribePattern 'espresense/rooms/livingroom/#' \
  unsubscribePattern 'espresense/rooms/livingroom/#' \
  publishClientReceive 'espresense/rooms/livingroom/#'

ensure_role "$RATGDO_ROLE" \
  publishClientSend 'homeassistant/+/Main_Garage_Door/#' \
  publishClientSend 'ratgdo_Main_Garage_Door/status/#' \
  subscribePattern 'homeassistant/status' \
  unsubscribePattern 'homeassistant/status' \
  publishClientReceive 'homeassistant/status' \
  subscribePattern 'ratgdo_Main_Garage_Door/command/#' \
  unsubscribePattern 'ratgdo_Main_Garage_Door/command/#' \
  publishClientReceive 'ratgdo_Main_Garage_Door/command/#'

ensure_role "$LEGACY_RATGDO_ROLE" \
  publishClientSend 'homeassistant/+/Main_Garage_Door/#' \
  publishClientSend 'homeassistant/+/Small_Garage_Door/#' \
  publishClientSend 'ratgdo_Main_Garage_Door/status/#' \
  publishClientSend 'ratgdo_Small_Garage_Door/status/#' \
  subscribePattern 'homeassistant/status' \
  unsubscribePattern 'homeassistant/status' \
  publishClientReceive 'homeassistant/status' \
  subscribePattern 'ratgdo_Main_Garage_Door/command/#' \
  unsubscribePattern 'ratgdo_Main_Garage_Door/command/#' \
  publishClientReceive 'ratgdo_Main_Garage_Door/command/#' \
  subscribePattern 'ratgdo_Small_Garage_Door/command/#' \
  unsubscribePattern 'ratgdo_Small_Garage_Door/command/#' \
  publishClientReceive 'ratgdo_Small_Garage_Door/command/#'

ensure_assignment "$HA_USER" "$HA_ROLE"
ensure_assignment "$BAR_USER" "$BAR_ROLE"
ensure_assignment "$LIVINGROOM_USER" "$LIVINGROOM_ROLE"
ensure_assignment "$RATGDO_USER" "$RATGDO_ROLE"
ensure_assignment "$LEGACY_RATGDO_USER" "$LEGACY_RATGDO_ROLE"
remove_obsolete_managed_roles

rm -f "$ADMIN_OPTIONS"
echo "Home Assistant migration MQTT clients and immutable v1 roles reconciled"
