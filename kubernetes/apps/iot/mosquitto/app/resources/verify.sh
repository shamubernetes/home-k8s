#!/bin/sh
set -eu

: "${MQTT_HOST:?MQTT_HOST is required}"
: "${MQTT_ADMIN_USERNAME:?MQTT_ADMIN_USERNAME is required}"
: "${MQTT_ADMIN_PASSWORD:?MQTT_ADMIN_PASSWORD is required}"

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

if mqtt_pub "$DEVICE_USERNAME" "$DEVICE_PASSWORD" \
  -t "${OTHER_ROOT}/reported" -m unauthorized >/dev/null 2>&1; then
  echo "Cross-device publish unexpectedly succeeded" >&2
  exit 1
fi
if mqtt_pub "$DEVICE_USERNAME" "$DEVICE_PASSWORD" \
  -t "${DEVICE_ROOT}/desired" -m unauthorized >/dev/null 2>&1; then
  echo "Device publish to desired unexpectedly succeeded" >&2
  exit 1
fi
mqtt_pub "$CONTROL_USERNAME" "$CONTROL_PASSWORD" \
  -t "${OTHER_ROOT}/desired" -r -m '{"release":"other-device"}'
if mqtt_sub "$DEVICE_USERNAME" "$DEVICE_PASSWORD" \
  -W 3 -C 1 -t "${OTHER_ROOT}/desired" >/dev/null 2>&1; then
  echo "Cross-device desired subscription unexpectedly succeeded" >&2
  exit 1
fi

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
mqtt_sub "$DEVICE_USERNAME" "$DEVICE_PASSWORD" \
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

echo "Mosquitto TLS, authentication, ACL, retained-message, reconnect, and LWT checks passed"
