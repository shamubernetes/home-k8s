# Mosquitto

Mosquitto is the shared MQTT broker for Zoo Fleet and future IoT products. It
is exposed only as TLS MQTT at `mqtt.${HOME_DOMAIN}:8883`. Cilium allocates the
LoadBalancer address from the cluster pool, and external-dns publishes the
address without a hardcoded IP.

## 1Password item

Create an item named `mosquitto` in the `Kubernetes` vault with these exact
concealed fields:

- `ADMIN_USERNAME`: the persistent Dynamic Security administrator username
- `ADMIN_PASSWORD`: a strong, unique administrator password

The administrator exists only for runtime management through the Dynamic
Security API. The initial configuration is created once on the retained PVC;
do not rename the administrator field after initialization. To rotate its
password, use `mosquitto_ctrl dynsec setClientPassword` over TLS first, verify
the new password, and then update the 1Password field.

## Device authorization

Zoo Fleet creates clients, groups, and roles at runtime. Each device:

- authenticates with a unique username equal to its Zoo Fleet device ID;
- belongs to a product/target-specific group;
- receives a product/target-specific role whose topic contains `%u`.

For product `PRODUCT`, target `TARGET`, and device username `%u`, the device
role grants only:

```text
subscribePattern       zoo/fleet/v1/products/PRODUCT/targets/TARGET/devices/%u/desired
publishClientReceive   zoo/fleet/v1/products/PRODUCT/targets/TARGET/devices/%u/desired
publishClientSend      zoo/fleet/v1/products/PRODUCT/targets/TARGET/devices/%u/reported
publishClientSend      zoo/fleet/v1/products/PRODUCT/targets/TARGET/devices/%u/presence
```

Dynamic Security defaults deny publish and subscribe operations that do not
match these ACLs. The bootstrap also changes the initial receive and
unsubscribe defaults to deny. Product and target must be literal path
components in the role. Do not create a global device role with wildcard
product or target components.

## Verification

The suspended CronJob runs a destructive-but-self-cleaning broker contract
test. It creates uniquely named temporary clients, a group, and roles, verifies
TLS authentication and the v1 ACL boundaries, exercises retained desired state
across a reconnect, and verifies a retained Last Will before removing the
temporary Dynamic Security objects and retained messages.

Run it after the broker and DNS are ready:

```bash
kubectl -n iot create job \
  --from=cronjob/mosquitto-verification \
  "mosquitto-verification-$(date +%s)"
kubectl -n iot logs -f job/mosquitto-verification-<timestamp>
```

The test connects to `mqtt.${HOME_DOMAIN}:8883` and validates the public
certificate hostname. It never disables TLS verification.
