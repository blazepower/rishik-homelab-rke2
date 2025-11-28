# Logging

## Loki + Promtail

[Loki](https://grafana.com/oss/loki/) is deployed as the centralized log aggregation system, with [Promtail](https://grafana.com/docs/loki/latest/clients/promtail/) as the log collector running on all cluster nodes.

### Configuration

#### Loki

- Deployed via Helm chart from `https://grafana.github.io/helm-charts`
- Installed in the `monitoring` namespace
- Chart version: 6.5.3
- Deployment mode: SingleBinary (minimal homelab setup)
- Log retention: 30 days (720h)
- Storage: 10Gi PersistentVolumeClaim using Longhorn StorageClass
- Authentication: Disabled
- Replication factor: 1
- Schema: v13 with TSDB store

#### Promtail

- Deployed via Helm chart from `https://grafana.github.io/helm-charts`
- Installed in the `monitoring` namespace
- Chart version: 6.15.4
- Runs as DaemonSet on all nodes
- Scrapes all pod logs automatically
- Supports annotation-based scraping (`promtail.io/scrape: "true"`)

### Resource Limits

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Loki      | 100m        | 500m      | 256Mi          | 512Mi        |
| Promtail  | 50m         | 200m      | 64Mi           | 128Mi        |

### Files

- `infrastructure/logging/loki/helmrepository-loki.yaml` - Loki Helm repository source
- `infrastructure/logging/loki/helmrelease-loki.yaml` - Loki Helm release configuration
- `infrastructure/logging/loki/promtail/helmrepository-promtail.yaml` - Promtail Helm repository source
- `infrastructure/logging/loki/promtail/helmrelease-promtail.yaml` - Promtail Helm release configuration
- `infrastructure/logging/kustomization.yaml` - Kustomization for logging resources

### Accessing Logs in Grafana

Loki is automatically configured as a data source in Grafana. To query logs:

1. Open Grafana at https://grafana.homelab
2. Navigate to **Explore** (compass icon in the sidebar)
3. Select **Loki** from the data source dropdown
4. Use LogQL to query logs

#### Example LogQL Queries

```logql
# All logs from a specific namespace
{namespace="monitoring"}

# Logs from a specific pod
{pod="kube-prometheus-stack-grafana-xyz"}

# Filter by container name
{container="grafana"}

# Search for errors
{namespace="monitoring"} |= "error"

# Filter by app label
{app="plex"}

# Logs from the last hour with rate
rate({namespace="kube-system"}[1h])
```

### Pod Log Scraping

Promtail is configured to scrape logs from all pods by default. You can control log scraping with annotations:

```yaml
# Opt-out of log scraping
metadata:
  annotations:
    promtail.io/scrape: "false"

# Opt-in for targeted scraping (kubernetes-pods job)
metadata:
  annotations:
    promtail.io/scrape: "true"
```

### Integration with Alerting

Loki includes built-in alerting rules that integrate with Prometheus Alertmanager:

- **LokiHighLatencyQueries**: Alerts when query latency exceeds 10 seconds at p99
- **LokiRequestErrors**: Alerts when error rate exceeds 10%

Additional Loki monitoring is included in the homelab PrometheusRules:

- **LokiNotReceivingLogs**: Alerts when Loki receives no logs for 15 minutes

### Health Probes

Loki is configured with liveness and readiness probes:

- **Endpoint**: `/ready`
- **Initial delay**: 45 seconds
- **Check interval**: 10 seconds
- **Failure threshold**: 3 attempts

### Storage Retention

Logs are retained for 30 days (720 hours). The compactor runs retention cleanup with a 2-hour delay to ensure safe deletion. Storage is backed by Longhorn PersistentVolume for durability.
