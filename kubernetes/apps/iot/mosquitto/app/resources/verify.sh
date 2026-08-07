#!/bin/sh
set -eu

: "${MQTT_HOST:?MQTT_HOST is required}"
: "${MQTT_ADMIN_USERNAME:?MQTT_ADMIN_USERNAME is required}"
: "${MQTT_ADMIN_PASSWORD:?MQTT_ADMIN_PASSWORD is required}"
: "${MQTT_HA_PASSWORD:?MQTT_HA_PASSWORD is required}"
: "${MQTT_ESPRESENSE_BAR_PASSWORD:?MQTT_ESPRESENSE_BAR_PASSWORD is required}"
: "${MQTT_ESPRESENSE_LIVINGROOM_PASSWORD:?MQTT_ESPRESENSE_LIVINGROOM_PASSWORD is required}"
: "${MQTT_RATGDO_MAIN_PASSWORD:?MQTT_RATGDO_MAIN_PASSWORD is required}"

MQTT_PORT=8883
CA_FILE=/etc/ssl/certs/ca-certificates.crt
RUN_ID="$(date +%s)-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
DEVICE_USERNAME="verify-device-${RUN_ID}"
CONTROL_USERNAME="verify-control-${RUN_ID}"
DEVICE_ROLE="verify-device-role-${RUN_ID}"
CONTROL_ROLE="verify-control-role-${RUN_ID}"
DEVICE_GROUP="verify-device-group-${RUN_ID}"
DEVICE_PASSWORD="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
CONTROL_PASSWORD="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
TOPIC_ROOT="zoo/fleet/v1/products/verify-product/targets/verify-target/devices"
DEVICE_ROOT="${TOPIC_ROOT}/${DEVICE_USERNAME}"
OTHER_ROOT="${TOPIC_ROOT}/not-${DEVICE_USERNAME}"
ADMIN_OPTIONS=/work/admin-options

printf '%s\n' \
  "--cafile ${CA_FILE}" \
  "-h ${MQTT_HOST}" \
  "-p ${MQTT_PORT}" \
  "-u ${MQTT_ADMIN_USERNAME}" \
  "-P ${MQTT_ADMIN_PASSWORD}" > "$ADMIN_OPTIONS"
chmod 0600 "$ADMIN_OPTIONS"

ctrl() {
  mosquitto_ctrl -o "$ADMIN_OPTIONS" dynsec "$@"
}

mqtt_pub() {
  username="$1"
  password="$2"
  shift 2
  mosquitto_pub \
    --cafile "$CA_FILE" \
    -h "$MQTT_HOST" \
    -p "$MQTT_PORT" \
    -V 5 \
    -u "$username" \
    -P "$password" \
    -q 1 \
    "$@"
}

mqtt_sub() {
  username="$1"
  password="$2"
  shift 2
  mosquitto_sub \
    --cafile "$CA_FILE" \
    -h "$MQTT_HOST" \
    -p "$MQTT_PORT" \
    -V 5 \
    -u "$username" \
    -P "$password" \
    -q 1 \
    "$@"
}

mqtt_plain_pub() {
  username="$1"
  password="$2"
  shift 2
  mosquitto_pub \
    -h "$MQTT_HOST" \
    -p 1883 \
    -V 5 \
    -u "$username" \
    -P "$password" \
    -q 1 \
    "$@"
}

mqtt_plain_sub() {
  username="$1"
  password="$2"
  shift 2
  mosquitto_sub \
    -h "$MQTT_HOST" \
    -p 1883 \
    -V 5 \
    -u "$username" \
    -P "$password" \
    -q 1 \
    "$@"
}

deny_sequence=0
expect_publish_blocked() {
  transport="$1"
  username="$2"
  password="$3"
  topic="$4"
  deny_sequence=$((deny_sequence + 1))
  output="/work/denied-publish-${deny_sequence}"

  mqtt_sub "$MQTT_ADMIN_USERNAME" "$MQTT_ADMIN_PASSWORD" \
    -W 3 -C 1 -t "$topic" > "$output" &
  observer=$!
  sleep 1
  if [ "$transport" = plain ]; then
    mqtt_plain_pub "$username" "$password" \
      -t "$topic" -m unauthorized >/dev/null 2>&1 || true
  else
    mqtt_pub "$username" "$password" \
      -t "$topic" -m unauthorized >/dev/null 2>&1 || true
  fi

  set +e
  wait "$observer"
  observer_status=$?
  set -e
  [ "$observer_status" -eq 27 ]
  [ ! -s "$output" ]
}

expect_subscription_blocked() {
  transport="$1"
  username="$2"
  password="$3"
  filter="$4"
  probe_topic="$5"
  deny_sequence=$((deny_sequence + 1))
  output="/work/denied-subscription-${deny_sequence}"

  if [ "$transport" = plain ]; then
    mqtt_plain_sub "$username" "$password" \
      -E -W 3 -C 1 -t "$filter" > "$output" 2>/dev/null &
  else
    mqtt_sub "$username" "$password" \
      -E -W 3 -C 1 -t "$filter" > "$output" 2>/dev/null &
  fi
  observer=$!
  sleep 1
  mqtt_pub "$MQTT_ADMIN_USERNAME" "$MQTT_ADMIN_PASSWORD" \
    -t "$probe_topic" -m unauthorized
  set +e
  wait "$observer"
  observer_status=$?
  set -e
  case "$observer_status" in
    0|27) ;;
    *) return 1 ;;
  esac
  [ ! -s "$output" ]
}

cleanup() {
  set +e
  mqtt_pub "$CONTROL_USERNAME" "$CONTROL_PASSWORD" \
    -t "${DEVICE_ROOT}/desired" -r -n >/dev/null 2>&1
  mqtt_pub "$CONTROL_USERNAME" "$CONTROL_PASSWORD" \
    -t "${OTHER_ROOT}/desired" -r -n >/dev/null 2>&1
  mqtt_pub "$DEVICE_USERNAME" "$DEVICE_PASSWORD" \
    -t "${DEVICE_ROOT}/reported" -r -n >/dev/null 2>&1
  mqtt_pub "$DEVICE_USERNAME" "$DEVICE_PASSWORD" \
    -t "${DEVICE_ROOT}/presence" -r -n >/dev/null 2>&1
  ctrl deleteClient "$DEVICE_USERNAME" >/dev/null 2>&1
  ctrl deleteClient "$CONTROL_USERNAME" >/dev/null 2>&1
  ctrl deleteGroup "$DEVICE_GROUP" >/dev/null 2>&1
  ctrl deleteRole "$DEVICE_ROLE" >/dev/null 2>&1
  ctrl deleteRole "$CONTROL_ROLE" >/dev/null 2>&1
  rm -f "$ADMIN_OPTIONS"
}
trap cleanup EXIT INT TERM

echo "Waiting for the TLS broker and Dynamic Security API"
attempt=0
until ctrl listClients >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "Broker did not become ready within five minutes" >&2
    exit 1
  fi
  sleep 5
done

echo "Verifying anonymous clients are rejected"
if mosquitto_pub \
  --cafile "$CA_FILE" \
  -h "$MQTT_HOST" \
  -p "$MQTT_PORT" \
  -V 5 \
  -q 1 \
  -t "${DEVICE_ROOT}/reported" \
  -m unauthorized >/dev/null 2>&1; then
  echo "Anonymous publish unexpectedly succeeded" >&2
  exit 1
fi

echo "Creating ephemeral Dynamic Security clients, group, roles, and ACLs"
printf '%s\n%s\n' "$DEVICE_PASSWORD" "$DEVICE_PASSWORD" |
  ctrl createClient "$DEVICE_USERNAME" >/dev/null
printf '%s\n%s\n' "$CONTROL_PASSWORD" "$CONTROL_PASSWORD" |
  ctrl createClient "$CONTROL_USERNAME" >/dev/null

ctrl createRole "$DEVICE_ROLE" >/dev/null
ctrl addRoleACL "$DEVICE_ROLE" subscribePattern \
  "zoo/fleet/v1/products/verify-product/targets/verify-target/devices/%u/desired" allow 100 >/dev/null
ctrl addRoleACL "$DEVICE_ROLE" publishClientReceive \
  "zoo/fleet/v1/products/verify-product/targets/verify-target/devices/%u/desired" allow 100 >/dev/null
ctrl addRoleACL "$DEVICE_ROLE" publishClientSend \
  "zoo/fleet/v1/products/verify-product/targets/verify-target/devices/%u/reported" allow 100 >/dev/null
ctrl addRoleACL "$DEVICE_ROLE" publishClientSend \
  "zoo/fleet/v1/products/verify-product/targets/verify-target/devices/%u/presence" allow 100 >/dev/null
ctrl createGroup "$DEVICE_GROUP" >/dev/null
ctrl addGroupRole "$DEVICE_GROUP" "$DEVICE_ROLE" 100 >/dev/null
ctrl addGroupClient "$DEVICE_GROUP" "$DEVICE_USERNAME" 100 >/dev/null

ctrl createRole "$CONTROL_ROLE" >/dev/null
ctrl addRoleACL "$CONTROL_ROLE" publishClientSend \
  "${DEVICE_ROOT}/desired" allow 100 >/dev/null
ctrl addRoleACL "$CONTROL_ROLE" publishClientSend \
  "${OTHER_ROOT}/desired" allow 100 >/dev/null
ctrl addRoleACL "$CONTROL_ROLE" subscribePattern \
  "${DEVICE_ROOT}/reported" allow 100 >/dev/null
ctrl addRoleACL "$CONTROL_ROLE" publishClientReceive \
  "${DEVICE_ROOT}/reported" allow 100 >/dev/null
ctrl addRoleACL "$CONTROL_ROLE" subscribePattern \
  "${DEVICE_ROOT}/presence" allow 100 >/dev/null
ctrl addRoleACL "$CONTROL_ROLE" publishClientReceive \
  "${DEVICE_ROOT}/presence" allow 100 >/dev/null
ctrl addClientRole "$CONTROL_USERNAME" "$CONTROL_ROLE" 100 >/dev/null

echo "Verifying the device can publish only its own reported state"
mqtt_sub "$CONTROL_USERNAME" "$CONTROL_PASSWORD" \
  -W 10 -C 1 -t "${DEVICE_ROOT}/reported" > /work/reported &
reported_subscriber=$!
sleep 1
mqtt_pub "$DEVICE_USERNAME" "$DEVICE_PASSWORD" \
  -t "${DEVICE_ROOT}/reported" -m '{"status":"ok"}'
wait "$reported_subscriber"
grep -qx '{"status":"ok"}' /work/reported

expect_publish_blocked tls "$DEVICE_USERNAME" "$DEVICE_PASSWORD" \
  "${OTHER_ROOT}/reported"
expect_publish_blocked tls "$DEVICE_USERNAME" "$DEVICE_PASSWORD" \
  "${DEVICE_ROOT}/desired"
mqtt_pub "$CONTROL_USERNAME" "$CONTROL_PASSWORD" \
  -t "${OTHER_ROOT}/desired" -r -m '{"release":"other-device"}'
expect_subscription_blocked tls "$DEVICE_USERNAME" "$DEVICE_PASSWORD" \
  "${OTHER_ROOT}/desired" "${OTHER_ROOT}/desired"

echo "Verifying retained desired state survives a device reconnect"
mqtt_pub "$CONTROL_USERNAME" "$CONTROL_PASSWORD" \
  -t "${DEVICE_ROOT}/desired" -r -m '{"release":"verify"}'
mqtt_sub "$DEVICE_USERNAME" "$DEVICE_PASSWORD" \
  -W 10 -C 1 -t "${DEVICE_ROOT}/desired" > /work/desired-first
grep -qx '{"release":"verify"}' /work/desired-first
mqtt_sub "$DEVICE_USERNAME" "$DEVICE_PASSWORD" \
  -W 10 -C 1 -t "${DEVICE_ROOT}/desired" > /work/desired-reconnect
grep -qx '{"release":"verify"}' /work/desired-reconnect

echo "Verifying an ungraceful disconnect publishes a retained Last Will"
mqtt_pub "$DEVICE_USERNAME" "$DEVICE_PASSWORD" \
  -t "${DEVICE_ROOT}/presence" -r -n
mosquitto_sub \
  --cafile "$CA_FILE" \
  -h "$MQTT_HOST" \
  -p "$MQTT_PORT" \
  -V 5 \
  -u "$DEVICE_USERNAME" \
  -P "$DEVICE_PASSWORD" \
  -q 1 \
  -i "verify-lwt-${RUN_ID}" \
  -t "${DEVICE_ROOT}/desired" \
  --will-topic "${DEVICE_ROOT}/presence" \
  --will-payload offline \
  --will-qos 1 \
  --will-retain > /work/lwt-client &
lwt_client=$!
sleep 2
kill -9 "$lwt_client"
wait "$lwt_client" 2>/dev/null || true

attempt=0
while :; do
  if mqtt_sub "$CONTROL_USERNAME" "$CONTROL_PASSWORD" \
    -W 3 -C 1 -t "${DEVICE_ROOT}/presence" > /work/presence 2>/dev/null &&
    grep -qx offline /work/presence; then
    break
  fi
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 10 ]; then
    echo "Retained Last Will was not observed" >&2
    exit 1
  fi
  sleep 1
done

echo "Verifying Home Assistant migration client compatibility and isolation"
HA_USER=home-assistant
BAR_USER=espresense-bar
LIVINGROOM_USER=espresense-livingroom
RATGDO_USER=ratgdo-main-garage

BAR_TOPIC="espresense/rooms/bar/verify/${RUN_ID}"
LIVINGROOM_TOPIC="espresense/rooms/livingroom/verify/${RUN_ID}"
RATGDO_STATUS_TOPIC="ratgdo_Main_Garage_Door/status/verify/${RUN_ID}"
RATGDO_COMMAND_TOPIC="ratgdo_Main_Garage_Door/command/verify/${RUN_ID}"
BAR_DISCOVERY_TOPIC="homeassistant/sensor/espresense_5c1fa8/verify/${RUN_ID}"
ESP_SETTING_TOPIC="espresense/settings/verify-${RUN_ID}/config"
ESP_BROADCAST_TOPIC="espresense/rooms/*/verify-${RUN_ID}/set"

mqtt_sub "$HA_USER" "$MQTT_HA_PASSWORD" -W 10 -C 1 -t "$BAR_TOPIC" > /work/ha-bar &
ha_bar_subscriber=$!
sleep 1
mqtt_plain_pub "$BAR_USER" "$MQTT_ESPRESENSE_BAR_PASSWORD" -t "$BAR_TOPIC" -m "$RUN_ID"
wait "$ha_bar_subscriber"
grep -qx "$RUN_ID" /work/ha-bar

mqtt_sub "$HA_USER" "$MQTT_HA_PASSWORD" -W 10 -C 1 -t "$LIVINGROOM_TOPIC" > /work/ha-livingroom &
ha_livingroom_subscriber=$!
sleep 1
mqtt_plain_pub "$LIVINGROOM_USER" "$MQTT_ESPRESENSE_LIVINGROOM_PASSWORD" \
  -t "$LIVINGROOM_TOPIC" -m "$RUN_ID"
wait "$ha_livingroom_subscriber"
grep -qx "$RUN_ID" /work/ha-livingroom

mqtt_sub "$HA_USER" "$MQTT_HA_PASSWORD" -W 10 -C 1 -t "$RATGDO_STATUS_TOPIC" > /work/ha-ratgdo &
ha_ratgdo_subscriber=$!
sleep 1
mqtt_plain_pub "$RATGDO_USER" "$MQTT_RATGDO_MAIN_PASSWORD" \
  -t "$RATGDO_STATUS_TOPIC" -m "$RUN_ID"
wait "$ha_ratgdo_subscriber"
grep -qx "$RUN_ID" /work/ha-ratgdo

mqtt_plain_sub "$RATGDO_USER" "$MQTT_RATGDO_MAIN_PASSWORD" \
  -W 10 -C 1 -t "$RATGDO_COMMAND_TOPIC" > /work/ratgdo-command &
ratgdo_command_subscriber=$!
sleep 1
mqtt_pub "$HA_USER" "$MQTT_HA_PASSWORD" -t "$RATGDO_COMMAND_TOPIC" -m "$RUN_ID"
wait "$ratgdo_command_subscriber"
grep -qx "$RUN_ID" /work/ratgdo-command

mqtt_sub "$HA_USER" "$MQTT_HA_PASSWORD" -W 10 -C 1 \
  -t 'homeassistant/#' > /work/ha-bar-discovery &
ha_bar_discovery_subscriber=$!
sleep 1
mqtt_plain_pub "$BAR_USER" "$MQTT_ESPRESENSE_BAR_PASSWORD" \
  -t "$BAR_DISCOVERY_TOPIC" -m "$RUN_ID"
wait "$ha_bar_discovery_subscriber"
grep -qx "$RUN_ID" /work/ha-bar-discovery

set +e
mqtt_sub "$HA_USER" "$MQTT_HA_PASSWORD" -W 3 -C 1 \
  -t 'homeassistant/#' > /work/ha-retired-discovery &
ha_retired_discovery_subscriber=$!
sleep 1
mqtt_pub "$MQTT_ADMIN_USERNAME" "$MQTT_ADMIN_PASSWORD" \
  -t "homeassistant/sensor/retired-verify-${RUN_ID}/config" -m "$RUN_ID"
wait "$ha_retired_discovery_subscriber"
ha_retired_discovery_status=$?
set -e
[ "$ha_retired_discovery_status" -eq 27 ]
[ ! -s /work/ha-retired-discovery ]

mqtt_plain_sub "$BAR_USER" "$MQTT_ESPRESENSE_BAR_PASSWORD" \
  -W 10 -C 1 -t "$ESP_SETTING_TOPIC" > /work/bar-setting &
bar_setting_subscriber=$!
sleep 1
mqtt_pub "$HA_USER" "$MQTT_HA_PASSWORD" -t "$ESP_SETTING_TOPIC" -m "$RUN_ID"
wait "$bar_setting_subscriber"
grep -qx "$RUN_ID" /work/bar-setting

mqtt_plain_sub "$LIVINGROOM_USER" "$MQTT_ESPRESENSE_LIVINGROOM_PASSWORD" \
  -W 10 -C 1 -t "$ESP_BROADCAST_TOPIC" > /work/livingroom-broadcast &
livingroom_broadcast_subscriber=$!
sleep 1
mqtt_pub "$HA_USER" "$MQTT_HA_PASSWORD" -t "$ESP_BROADCAST_TOPIC" -m "$RUN_ID"
wait "$livingroom_broadcast_subscriber"
grep -qx "$RUN_ID" /work/livingroom-broadcast

if mosquitto_pub -h "$MQTT_HOST" -p 1883 -V 5 -q 1 \
  -t "$BAR_TOPIC" -m unauthorized >/dev/null 2>&1; then
  echo "Anonymous plaintext publish unexpectedly succeeded" >&2
  exit 1
fi
expect_publish_blocked plain "$BAR_USER" "$MQTT_ESPRESENSE_BAR_PASSWORD" \
  "$LIVINGROOM_TOPIC"
expect_subscription_blocked plain \
  "$LIVINGROOM_USER" "$MQTT_ESPRESENSE_LIVINGROOM_PASSWORD" \
  "$BAR_TOPIC" "$BAR_TOPIC"
expect_publish_blocked plain "$RATGDO_USER" "$MQTT_RATGDO_MAIN_PASSWORD" \
  "ratgdo_Small_Garage_Door/status/verify/${RUN_ID}"
for denied_topic in \
  "zoo/fleet/v1/products/verify/targets/verify/devices/verify/desired" \
  "ps5-mqtt/verify/${RUN_ID}" \
  "weatherflow/verify/${RUN_ID}" \
  "node-red/verify/${RUN_ID}"; do
  expect_publish_blocked tls "$HA_USER" "$MQTT_HA_PASSWORD" "$denied_topic"
done
expect_subscription_blocked tls "$HA_USER" "$MQTT_HA_PASSWORD" \
  '#' "retired/verify/${RUN_ID}"

echo "Mosquitto TLS, authenticated compatibility, ACL isolation, retained-message, reconnect, and LWT checks passed"
