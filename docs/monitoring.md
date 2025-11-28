# Monitoring

## kube-prometheus-stack

[kube-prometheus-stack](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack) is deployed to provide comprehensive cluster monitoring with Prometheus, Grafana, and Alertmanager.

### Configuration

- Deployed via Helm chart from `https://prometheus-community.github.io/helm-charts`
- Installed in the `monitoring` namespace
- Prometheus retention: 7 days
- Scrape interval: 30 seconds
- Grafana exposed via Traefik ingress at `https://grafana.homelab`
- Alertmanager enabled
- Loki configured as additional data source for log querying

### Resource Limits

All monitoring components have resource limits configured to prevent resource exhaustion:

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Prometheus | 100m | 500m | 256Mi | 1Gi |
| Grafana | 50m | 200m | 128Mi | 256Mi |
| Alertmanager | 25m | 100m | 64Mi | 128Mi |
| Prometheus Operator | 50m | 200m | 64Mi | 256Mi |
| kube-state-metrics | 25m | 100m | 32Mi | 128Mi |
| node-exporter | 25m | 100m | 32Mi | 64Mi |

### Alerting with PrometheusRules

Comprehensive alerting is configured via PrometheusRules in `prometheusrules-alerts.yaml`. Alert groups include:

#### Node Health
- **NodeNotReady**: Node has been not ready for more than 5 minutes
- **NodeHighMemoryUsage**: Memory usage exceeds 85%
- **NodeHighCPUUsage**: CPU usage exceeds 85% for 10 minutes
- **NodeDiskPressure**: Disk space below 15%
- **NodeDiskCritical**: Disk space below 5%

#### Pod Health
- **PodCrashLooping**: Pod has restarted more than 5 times in an hour
- **PodNotReady**: Pod in Pending/Unknown/Failed state for 15 minutes
- **ContainerHighMemoryUsage**: Container using more than 90% of memory limit
- **ContainerHighCPUUsage**: Container using more than 90% of CPU limit

#### Deployment Health
- **DeploymentReplicasMismatch**: Deployment replicas don't match for 10 minutes
- **DaemonSetNotScheduled**: DaemonSet pods not fully scheduled

#### Storage Health
- **PVCAlmostFull**: PVC usage exceeds 85%
- **PVCCriticallyFull**: PVC usage exceeds 95%
- **LonghornVolumeActualSpaceLow**: Longhorn volume using more than 85% capacity

#### Flux Health
- **FluxReconciliationFailure**: Flux reconciliation failing for 10 minutes

#### Monitoring Health
- **PrometheusTargetMissing**: Prometheus scrape target is down
- **LokiNotReceivingLogs**: Loki not receiving logs for 15 minutes
- **AlertmanagerNotReceivingAlerts**: No alerts received in 30 minutes

### Data Sources

Grafana is configured with the following data sources:

| Name | Type | URL |
|------|------|-----|
| Prometheus | prometheus | (internal) |
| Loki | loki | http://loki.monitoring.svc.cluster.local:3100 |

The Loki data source enables log querying directly from Grafana's Explore interface. See [docs/logging.md](logging.md) for LogQL query examples.

### Files

- `infrastructure/monitoring/helmrepository-prometheus-community.yaml` - Prometheus community Helm repository
- `infrastructure/monitoring/helmrepository-grafana.yaml` - Grafana Helm repository
- `infrastructure/monitoring/helmrelease-kube-prometheus-stack.yaml` - Helm release configuration
- `infrastructure/monitoring/prometheusrules-alerts.yaml` - PrometheusRules for alerting
- `infrastructure/monitoring/ingress-grafana.yaml` - Ingress for Grafana UI (HTTPS)
- `infrastructure/monitoring/kustomization.yaml` - Kustomization for monitoring resources

### Accessing Grafana

Grafana is accessible via the Traefik ingress at https://grafana.homelab. Ensure you have:
1. A DNS entry pointing `grafana.homelab` to your Traefik ingress controller's IP address
2. The homelab CA certificate installed on your device (see [docs/tls.md](tls.md))

Alternatively, use port-forwarding:
```bash
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
```
Then open http://localhost:3000 in your browser.

### Grafana Credentials

Grafana admin credentials are stored in a Kubernetes secret:
- Secret name: `grafana-admin-credentials`
- Username: `admin`
- Password key: `admin-password`

### Customization

To pin a specific chart version, uncomment and set the `version` field in `helmrelease-kube-prometheus-stack.yaml`.
