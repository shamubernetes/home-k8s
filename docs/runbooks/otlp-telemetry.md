# OTLP telemetry platform runbook

This runbook operates the shared OTLP/HTTP gateway deployed by K8S-17. The architecture and trade-offs are recorded in [`../decisions/otlp-telemetry-platform.md`](../decisions/otlp-telemetry-platform.md).

## Components

| Component | Purpose | Internal endpoint |
|---|---|---|
| OpenTelemetry Collector | Authenticated OTLP/HTTP gateway | `https://otlp.${HOME_DOMAIN}` |
| Prometheus | Metrics storage | Scrapes collector port `8889` through ServiceMonitor |
| VictoriaLogs | Log storage | `http://victoria-logs.observability.svc:9428` |
| VictoriaTraces | Trace storage and query | `http://victoria-traces.observability.svc:10428` |
| Grafana | Query and correlation | `https://grafana.${HOME_DOMAIN}` |

Producer endpoints are the normal OTLP/HTTP paths:

- `/v1/metrics`
- `/v1/logs`
- `/v1/traces`

The route is attached only to `envoy-internal`. The collector requires the per-producer bearer credential stored in 1Password.

## Hermes configuration

The credential lives in 1Password item `Kubernetes/otel-collector`. Set the top-level Hermes environment entry `HERMES_OTLP_AUTHORIZATION` to the 1Password reference for that item's `HERMES_AUTHORIZATION` field. Do not copy the credential value into `config.yaml`.

```yaml
monitoring:
  gateway_health_export:
    enabled: true
    metrics_enabled: true
    diagnostic_events_enabled: true
    warning_error_events_enabled: true
    export_interval_seconds: 60
    logs_export_interval_seconds: 60
    resource_attributes:
      service.name: hermes-gateway
      service.namespace: maude
  export:
    otlp:
      enabled: true
      endpoint: https://otlp.${HOME_DOMAIN}/v1/traces
      headers_env:
        Authorization: HERMES_OTLP_AUTHORIZATION
```

The trace endpoint is the configured base signal endpoint. Hermes derives `/v1/metrics` and `/v1/logs` from it.

After changing configuration, restart the Hermes gateway and verify:

```bash
hermes monitoring status
hermes gateway status
```

## Health checks

```bash
kubectl -n observability get pods,svc,httproute \
  -l 'app.kubernetes.io/name in (otel-collector,victoria-traces)'

kubectl -n observability get externalsecret otel-collector-auth
kubectl -n observability get replicationsource victoria-traces victoria-traces-r2

curl -fsS http://victoria-traces.observability.svc.cluster.local:10428/health
curl -fsS http://victoria-traces.observability.svc.cluster.local:10428/select/tempo/api/search/tags
```

Expected state:

- Two collector replicas are ready on different nodes.
- One VictoriaTraces replica is ready with PVC `server-volume-victoria-traces-0`.
- `otel-collector-auth` is `Ready=True` and `SecretSynced`.
- The internal HTTPRoute is accepted and resolved.
- Prometheus targets for both applications are up.
- Grafana data source `VictoriaTraces` reports success.

## End-to-end synthetic test

Use any OTLP/HTTP test client with a temporary service name and the stored authorization header. Send one metric, log, and span, then verify each backend independently:

1. Prometheus contains the synthetic metric with the expected `service_name` resource label.
2. VictoriaLogs returns the synthetic log through LogsQL.
3. VictoriaTraces returns the synthetic trace through `/select/tempo` or `/select/jaeger`.
4. Grafana Explore can open the trace, pivot to logs by `trace_id`, and open the gateway-health metric link.

For Hermes, the repository's probe exercises the real exporter against a capture collector:

```bash
python scripts/observability/gateway_health_export_probe.py \
  --endpoint http://127.0.0.1:4318/v1/traces \
  --log /tmp/hermes-otel-capture.jsonl --wait 8
```

A successful probe reports requests to all three OTLP paths. Production verification must still query the real backends after cutover.

## Onboard another producer

1. Confirm the application's exported attributes contain no unexpected secrets or high-cardinality content.
2. Add a distinct bearer token to the 1Password item and the collector authentication token list.
3. Add the producer's trusted network path. Prefer the internal TLS route rather than direct Service access.
4. Set stable `service.name`, `service.namespace`, `service.version`, and `deployment.environment.name` resource attributes.
5. Send synthetic signals and verify each backend independently.
6. Add service-specific alerts only after observing real metric and attribute names.

Do not reuse the Hermes token for another producer.

## Capacity and retention

- Online trace retention: 14 days.
- VictoriaTraces PVC: 30 GiB.
- Application storage cap: 25 GiB.
- Warning at 70% PVC use, critical at 85%.
- Collector memory limit: 1 GiB per replica.

If storage growth reaches warning:

1. Check ingestion rate and unexpected cardinality.
2. Confirm retention is deleting old partitions.
3. Reduce noisy attributes or sampling before expanding storage.
4. Expand the Ceph RBD PVC only when the data is useful and expected.

## Backup and recovery

VolSync creates:

- Hourly NAS backups, retaining 24 hourly and 7 daily copies.
- Daily R2 backups, retaining 7 daily and 4 weekly copies.

Target RPO is one hour locally and 24 hours offsite. Target RTO is 30 minutes.

Recovery procedure:

1. Disable or block OTLP ingestion if the volume is inconsistent.
2. Suspend the `victoria-traces` Flux Kustomization.
3. Restore `server-volume-victoria-traces-0` from the selected VolSync restore point.
4. Resume and reconcile `victoria-traces`.
5. Wait for the StatefulSet and Prometheus target to become healthy.
6. Send a synthetic trace and query it through VictoriaTraces and Grafana.
7. Re-enable ingestion.

If the restore cannot meet the RTO, create an empty 30 GiB PVC through GitOps and resume ingestion. Historical traces may be discarded because they are non-authoritative operational data.

## Rollback

1. Disable `monitoring.export.otlp.enabled` in Hermes and restart the gateway.
2. Confirm Hermes remains healthy and no new collector requests arrive.
3. Remove or disable the internal HTTPRoute.
4. Keep VictoriaTraces, its PVC, and backups until replacement verification finishes.
5. Repoint collector exporters and the Grafana data source if changing only the backend.
6. Remove the GitOps applications last.

Collector export failures are fail-isolated from Hermes. A telemetry backend outage must not become a gateway outage.
