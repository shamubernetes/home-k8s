# OTLP telemetry and tracing platform

- Status: Accepted
- Date: 2026-08-11
- Kaneo: K8S-17, K8S-18

## Context

Hermes can export content-free gateway health metrics, diagnostic logs, and lifecycle spans over OTLP/HTTP. The cluster already runs Grafana, Prometheus, VictoriaLogs, VictoriaMetrics, Ceph RBD, VolSync, and redundant Ceph RGW. It does not run an OTLP receiver or trace store.

The platform must remain useful for future applications without replacing Prometheus or VictoriaLogs. It also needs bounded storage, internal authenticated ingress, GitOps delivery, monitoring, backups, and a tested rollback path.

## Decision

Deploy the following in the `observability` namespace:

1. Upstream OpenTelemetry Collector Contrib as a two-replica OTLP/HTTP gateway.
2. A Prometheus exporter and ServiceMonitor for metrics.
3. VictoriaLogs native OTLP ingestion for logs.
4. VictoriaTraces single-node for traces.
5. Grafana's built-in Tempo data source against VictoriaTraces' Tempo-compatible query API.

The collector is exposed only through the internal Envoy Gateway at `otlp.${HOME_DOMAIN}`. TLS terminates at Envoy and the collector validates a bearer token supplied from 1Password through External Secrets. Backend services remain cluster-local.

VictoriaTraces uses a 30 GiB Ceph RBD PVC, 14-day retention, and a 25 GiB storage cap. VolSync creates hourly NAS backups and daily R2 backups using the existing `volsync-template` credentials. The initial resource envelope is 100m CPU and 256 MiB memory requested, with limits of 2 CPU and 2 GiB.

## Rationale

### OpenTelemetry Collector Contrib over Grafana Alloy

The upstream collector keeps configuration and pipelines in the OpenTelemetry standard, has the receivers, authentication extension, processors, and exporters required here, and avoids a Grafana-specific configuration layer. Alloy remains a valid future choice if Prometheus-style discovery or Grafana-specific integrations become a dominant requirement.

### VictoriaTraces over Tempo

Tempo 3.0 requires Kafka or a Kafka-compatible queue in every deployment mode. Adding a queue solely for tracing is disproportionate for this cluster. VictoriaTraces has no external database or queue dependency, supports native OTLP ingestion, and implements Tempo and Jaeger query APIs for Grafana.

VictoriaTraces is still pre-1.0. That maturity risk is accepted because trace data is operational telemetry, not authoritative data, and the deployment has bounded storage, backups, and a clean replacement boundary at the collector exporter and Grafana data source.

### VictoriaTraces over Jaeger

Jaeger v2 uses OpenTelemetry Collector components but still requires an external production storage backend such as Cassandra, Elasticsearch, or OpenSearch. None of those systems otherwise belongs in this cluster.

### Single-node VictoriaTraces over the cluster chart

Ceph already replicates the block volume. A single VictoriaTraces process minimizes operational cost and can be rescheduled with the same RBD volume after node failure. A brief query or ingestion interruption during restart is acceptable. The collector retries transient backend failures.

Move to VictoriaTraces cluster mode only when sustained ingestion, query load, or availability evidence exceeds this design. Do not scale components preemptively.

## Signal routing

| Signal | Collector exporter | Backend |
|---|---|---|
| Metrics | Prometheus exposition | Prometheus scrape through ServiceMonitor |
| Logs | OTLP/HTTP | VictoriaLogs `/insert/opentelemetry/v1/logs` |
| Traces | OTLP/HTTP | VictoriaTraces `/insert/opentelemetry/v1/traces` |

Grafana queries traces through `http://victoria-traces.observability.svc.cluster.local:10428/select/tempo`. Trace-to-log and trace-to-metric links use the existing VictoriaLogs and Prometheus data-source UIDs.

## Security and tenancy

- The public Envoy gateway is not attached.
- The internal route is TLS-only.
- The collector rejects requests without an allowed bearer token.
- Tokens live only in 1Password and generated Kubernetes Secrets.
- NetworkPolicies restrict the receiver to the Envoy and observability namespaces and restrict backends to observability clients.
- New producers receive a distinct token by extending the collector token list. Backend tenancy is deferred until multiple trust domains exist.
- Hermes exports a fixed, enumerated, content-free monitoring vocabulary. Future producers must classify and limit attributes before onboarding.

## Retention, backup, and recovery

- Online retention: 14 days.
- Online capacity: 30 GiB PVC with a 25 GiB application cap.
- NAS backup: hourly, 24 hourly and 7 daily restore points.
- R2 backup: daily, 7 daily and 4 weekly restore points.
- Target RPO: one hour locally, 24 hours offsite.
- Target RTO: 30 minutes for GitOps redeploy plus PVC restore.

Recovery is: suspend ingestion if needed, restore the VictoriaTraces PVC from VolSync, reconcile the application, verify health, send a synthetic trace, and confirm Grafana queryability. If recovery fails, redeploy with an empty PVC and accept loss of historical traces. Metrics and logs remain independently available.

## Rollback and replacement boundaries

- Disable Hermes OTLP export first. This is fail-open and does not affect gateway operation.
- Remove the internal route or bearer token to stop new ingestion.
- Repoint the collector trace exporter and Grafana data source to replace VictoriaTraces without changing producers.
- Repoint individual metric or log exporters independently if those stores change.
- Keep the PVC and backups until end-to-end verification of the replacement is complete.

## Sources

- OpenTelemetry Collector gateway deployment: https://opentelemetry.io/docs/collector/deployment/gateway/
- OpenTelemetry Collector Helm chart: https://opentelemetry.io/docs/platforms/kubernetes/helm/collector/
- Collector bearer-token authentication: https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/extension/bearertokenauthextension
- VictoriaTraces: https://docs.victoriametrics.com/victoriatraces/
- VictoriaTraces Grafana integration: https://docs.victoriametrics.com/victoriatraces/querying/grafana/
- VictoriaLogs OTLP ingestion: https://docs.victoriametrics.com/victorialogs/data-ingestion/opentelemetry/
- Tempo 3.0 deployment requirements: https://grafana.com/docs/tempo/latest/setup/deployment/
- Jaeger v2 architecture: https://www.jaegertracing.io/docs/latest-v2/architecture/
- Hermes monitoring export: https://github.com/NousResearch/hermes-agent/blob/main/docs/observability/monitoring.md
