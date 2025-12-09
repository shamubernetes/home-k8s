# CloudNative-PG

PostgreSQL cluster managed by CloudNative-PG operator with ceph-block storage and automated backups.

## Backup Strategy

The cluster uses a multi-layer backup strategy:

1. **Continuous WAL Archiving**: All Write-Ahead Logs are continuously streamed to R2 (offsite)
2. **Scheduled R2 Backups**: Base backups to R2 every 12 hours
3. **Volume Snapshots**: Daily Ceph block snapshots at 2:00 AM
4. **Point-in-Time Recovery (PITR)**: Can restore to any point in time using WAL logs + base backups

## Storage

- **Data Storage**: `ceph-block` - Ceph RBD with snapshot support
- **WAL Storage**: `ceph-block` - Separate volume for write-ahead logs
- **Snapshot Class**: `csi-ceph-blockpool` - For volume snapshots

## Connection Pooler (PgBouncer)

A PgBouncer pooler is deployed for better connection handling during failovers:

- **`postgres17-pooler`**: **Use this for all applications** - Handles failover gracefully
- `postgres17-rw`: Direct connection to primary (use only for admin/monitoring)
- `postgres17-ro`: Read-only service (replicas)
- `postgres17-r`: Any instance (for reads that can tolerate stale data)
- `postgres-lb`: LoadBalancer service for external access

The pooler:
- Keeps client connections alive during primary switchovers
- Uses transaction pooling mode for optimal failover handling
- Automatically reconnects to the new primary without dropping client connections

## Recovery Procedures

### Recover from a Catastrophic Failure

If the entire cluster is lost, create a new cluster that recovers from the latest R2 backup:

1. Update `cluster17.yaml` to include the bootstrap recovery section:

```yaml
bootstrap:
  recovery:
    source: &previousCluster postgres17-v1
externalClusters:
- name: *previousCluster
  plugin:
    name: barman-cloud.cloudnative-pg.io
    parameters:
      barmanObjectName: r2
      serverName: *previousCluster
```

2. Increment the `serverName` in the ObjectStore (e.g., `postgres17-v2`)

3. Apply the changes and wait for the cluster to recover

4. After successful recovery, comment out the `bootstrap` section

### Point-in-Time Recovery

To recover to a specific point in time:

```yaml
bootstrap:
  recovery:
    source: postgres17-v1
    recoveryTarget:
      targetTime: "2024-01-15 10:30:00.000000+00"
```

### Upgrade PostgreSQL Version

When upgrading major PostgreSQL versions (e.g., 16 → 17):

1. Ensure the old cluster has a recent backup
2. Create a new cluster file with the new version
3. Configure the new cluster to recover from the old cluster's backup
4. Update all applications to point to the new cluster service name
5. After successful migration, comment out the `bootstrap` section and delete the old cluster

## Monitoring

- Pod monitors enabled for Prometheus metrics
- Grafana dashboard installed via operator
- PrometheusRule for alerting
