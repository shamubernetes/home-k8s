# Mosquitto

Mosquitto is the shared MQTT broker for Zoo Fleet and retained Home Assistant devices. Cilium allocates the LoadBalancer address from the cluster pool, and external-dns publishes it without a hardcoded IP.

- TLS MQTT: `mqtt.${HOME_DOMAIN}:8883`
- Authenticated migration compatibility: `mqtt.${HOME_DOMAIN}:1883`

The plaintext listener exists only because the retained ESPresense and native ratgdo firmware do not support MQTT TLS. NetworkPolicy and CiliumNetworkPolicy restrict it to ESPresense Bar (`10.107.104.20`), ESPresense Living Room (`10.107.107.175`), Main Garage ratgdo (`10.107.106.236`), and explicitly labeled verifier pods in `iot`. Cilium DSR preserves external client addresses for those `/32` checks; do not set `externalTrafficPolicy: Local`, which is incompatible with the cluster's L2 announcements. Anonymous access remains disabled. Small Garage, retired ESPresense rooms, WeatherFlow, PS5 MQTT, and Node-RED are deliberately excluded.

## 1Password item

The `mosquitto` item in the `Kubernetes` vault has these exact concealed fields:

- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `HA_PASSWORD`
- `ESPRESENSE_BAR_PASSWORD`
- `ESPRESENSE_LIVINGROOM_PASSWORD`
- `RATGDO_MAIN_PASSWORD`

External Secrets creates `mosquitto-secret`. The broker init flow reconciles the retained Dynamic Security administrator hash from that Secret before every broker start. This preserves Zoo Fleet clients and roles while preventing 1Password and the retained JSON file from drifting apart.

## Device authorization

Zoo Fleet creates its own clients, groups, and roles at runtime. Each device authenticates with a unique username and receives product/target-specific ACLs containing `%u`.

The `mosquitto-home-assistant-clients-v2` Job provisions four fixed clients and immutable v1 roles. It removes obsolete `ha-migration-*` role versions after assigning the current roles, without touching Zoo Fleet state:

- Home Assistant, TLS `8883`
- ESPresense Bar, authenticated `1883`
- ESPresense Living Room, authenticated `1883`
- Main Garage ratgdo, authenticated `1883`

Each retained device is limited to its own discovery, room, state, command, and ESPresense configuration topic roots. Home Assistant can receive only those three devices and can publish only `homeassistant/status`, ESPresense room/configuration topics, and Main Garage commands.

Dynamic Security denies publish and subscribe operations that do not match an ACL. Explicit receive and unsubscribe ACLs keep the roles valid if the broker's corresponding defaults are deny.

## Credential rotation

For the administrator password:

1. Update `ADMIN_PASSWORD` in 1Password.
2. Restart the Mosquitto pod so the offline init reconciliation updates the retained Dynamic Security file.
3. Run the verification Job.

For a Home Assistant migration client password, update the matching 1Password field and increment the provisioning Job version so Flux runs it once. Increment the role version as well when ACLs change. Verify the broker before configuring a physical client.

## Retained-state migration

`app/resources/retained-allowlist.txt` is the only approved retained-topic scope. Export and import are rehearsed against a disposable broker before cutover. Never copy the legacy `mosquitto.db` into this broker.

## Verification

The suspended CronJob runs a destructive-but-self-cleaning broker contract test. It never publishes into a physical device command hierarchy; command authorization is checked from the Dynamic Security role contract. It verifies:

- public TLS certificate and authentication;
- Zoo Fleet v1 ACL, reconnect, retained desired state, and retained Last Will behavior;
- Home Assistant TLS access;
- authenticated ESPresense and ratgdo compatibility on `1883`;
- cross-device, Small Garage, Zoo Fleet, PS5, WeatherFlow, and Node-RED denials.

Run it after the broker and DNS are ready:

```bash
kubectl -n iot create job \
  --from=cronjob/mosquitto-verification \
  "mosquitto-verification-$(date +%s)"
kubectl -n iot logs -f job/mosquitto-verification-<timestamp>
```

The test never disables TLS verification and removes its temporary Dynamic Security objects and retained messages.
