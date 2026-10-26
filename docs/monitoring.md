# Monitoring

## kube-prometheus-stack

[kube-prometheus-stack](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack) is deployed to provide comprehensive cluster monitoring with Prometheus, Grafana, and Alertmanager.

### Configuration

- Deployed via Helm chart from `https://prometheus-community.github.io/helm-charts`
- Installed in the `monitoring` namespace
- Prometheus retention: 7 days
- Scrape interval: 60 seconds
- Grafana exposed via Traefik ingress at `https://grafana.homelab`
- Alertmanager enabled
- Loki configured as additional data source for log querying

### Resource Limits

All monitoring components have resource limits configured to prevent resource exhaustion:

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Prometheus | 100m | 500m | 256Mi | 1Gi |
| Grafana | 100m | 500m | 512Mi | 1Gi |
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
- **LonghornVolumeDegraded/Faulted**: Longhorn volume robustness remains degraded or faulted
- **LonghornRecurringBackupFailed/Stuck**: A recurring backup errors or remains incomplete for 4 hours
- **LonghornVolumeBackupStale**: A previously backed-up volume has no successful backup for 36 hours
- **LonghornDiskSmartFailed**: Longhorn's disk health collector reports a SMART failure
- **StorageContainerOOMKilled/RestartPressure**: Critical Longhorn or CSI containers OOM or restart repeatedly

#### Controller and Filesystem Health
- **ControllerUnreclaimableKernelMemoryHigh/Critical**: Controller `SUnreclaim` exceeds 1 GiB or 4 GiB
- **NodeReadonlyFilesystem**: Node Problem Detector observes a read-only filesystem remount
- **NodeFilesystemIOError**: Node Problem Detector observes an ext4 or buffer I/O error

The existing **NodeNotReady** alert covers all nodes, including `rishik-worker1`.

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
- `infrastructure/monitoring/prometheusrules-storage-health-alerts.yaml` - Storage failure-containment alerts
- `infrastructure/monitoring/servicemonitor-longhorn.yaml` - Longhorn manager metrics discovery
- `infrastructure/monitoring/ingress-grafana.yaml` - Ingress for Grafana UI (HTTPS)
- `infrastructure/monitoring/kustomization.yaml` - Kustomization for monitoring resources
- `infrastructure/monitoring/custom-dashboards/` - Custom Grafana dashboards as ConfigMaps

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

Set the chart's exact `version` in `helmrelease-kube-prometheus-stack.yaml`.

### Chart upgrades and rollback

The 88.6.5 → 92.13.1 upgrade includes Operator 0.94.1, Prometheus 3.15.0,
Alertmanager 0.34.1, and Grafana chart 14.0.1 / Grafana 13.2.4-distroless.
The parent chart and Grafana require Kubernetes >= 1.25; validation used
the cluster's Kubernetes 1.34.12. Review these upstream changes:

- [Chart migration guide at the target tag](https://github.com/prometheus-community/helm-charts/blob/kube-prometheus-stack-92.13.1/charts/kube-prometheus-stack/UPGRADE.md):
  chart 90 moves control-plane ServiceMonitor credentials to a chart-created,
  long-lived service-account token Secret and the `kube-root-ca.crt` ConfigMap;
  chart 92 adds Linux node selectors and rolls monitoring workloads once.
- [Grafana chart migration guide](https://github.com/grafana-community/helm-charts/tree/grafana-14.0.1/charts/grafana#upgrading):
  chart 13 enables a read-only root filesystem, a writable temporary volume,
  and the distroless image; chart 14 validates values types.
  The existing credential Secret, dashboard sidecars, and provisioned
  Prometheus/Loki data sources remain in use. No unsupported `GF_*__FILE` or
  legacy plugin-install environment overrides are configured.
- [Prometheus 3.15 changelog](https://github.com/prometheus/prometheus/blob/v3.15.0/CHANGELOG.md):
  this upgrade does not enable new TSDB encoding or experimental storage flags.

Flux owns CRD upgrades through `spec.upgrade.crds: CreateReplace`; the chart's
separate CRD upgrade Job remains disabled. Flux updates CRD definitions before
the Helm upgrade without deleting removed CRDs. Plain Helm's default would
leave schemas stale (the pre-upgrade cluster still had Operator 0.86.2 CRDs).
All ten CRDs retain their existing storage API versions (`v1` or `v1alpha1`).
Never delete CRDs to resolve an upgrade failure: that deletes their instances.

Upgrade and rollback explicitly disable Helm server-side apply for this
migration. An SSA dry run merged the existing `storage.emptyDir` with the new
PVC volume and was correctly rejected as an invalid volume union. Helm's
client-side three-way strategic patch removes the old `emptyDir`; the exact
resulting Deployment and that removal patch are validated with server dry runs.
This does not use force-replacement or delete/recreate any resource. Flux
helm-controller1.6.5 supports the explicit `serverSideApply: disabled` policy.

Before merging, render both exact chart versions with the release's values,
namespace `monitoring`, release name `kube-prometheus-stack`,
`--kube-version 1.34.12`, and `--include-crds`. Run:

```powershell
python .github\scripts\test-monitoring-stack-upgrade.py --baseline baseline-render.yaml --target target-render.yaml
```

This requires PyYAML (also installed by yamllint). Additionally validate the
target CRDs with a server-side dry run, all existing monitoring custom
resources against the target CRD schemas, and all generated/custom rules with
the target Prometheus `promtool check rules`. Do not export Secret payloads.

Deployment remains an operator-controlled Flux change. Expect a temporary
monitoring gap while single-replica workloads roll. Verify the HelmRelease
Ready condition, all ten CRD version annotations, Prometheus/Alertmanager and
Grafana readiness, Grafana login and dashboard/data-source provisioning, and
the apiserver/kubelet scrape targets. In particular, verify the new token
Secret is populated using **key names only**, never by displaying its values.

Prometheus retains the same 10Gi Longhorn RWO claim, seven-day retention,
resource/query limits, and release/resource names. Do not delete or recreate
its PVC. Grafana switches from ephemeral storage to the **pre-seeded**
`grafana-data` claim described below. Alertmanager remains ephemeral; its
native silence export must be retained and imported if nonempty.

For rollback, revert the chart version through Git/Flux while preserving the
CRDs, monitoring custom resources, Prometheus PVC, and **Grafana's
`persistence.existingClaim: grafana-data` binding**. Revert the version, not
the entire preservation change. Do not assume Helm's
automatic rollback rolls back CRD schemas. A Git version revert with
`CreateReplace` can reapply the older chart's CRDs; keep the newer, compatible
CRDs by temporarily setting `upgrade.crds: Skip` for the downgrade, then review
the intended CRD lifecycle policy before a subsequent forward upgrade. Check
Prometheus logs and TSDB compatibility before downgrading its image; do not
enable new storage encodings during this upgrade.

### Preserving Grafana and Alertmanager state before the upgrade

Phase A ([PR333](https://github.com/blazepower/rishik-homelab-rke2/pull/333))
only provisions `monitoring/grafana-data`: 2Gi RWO on the existing
`longhorn-retain` class (two replicas, weekly snapshots, daily off-cluster
backups). The claim is Flux-prune-protected. Merge/reconcile that phase and
wait for Bound **without upgrading Grafana**.

The helper assets in `infrastructure/monitoring/preservation/` are deliberately
**not** in the monitoring Flux Kustomization. Never auto-reconcile them.
The coordinator runs `.github/scripts/preserve-grafana-state.py`; its default
`plan` is read-only against the cluster and records only route identities/specs
and aggregate Grafana/plugin metadata locally. No DB, silence payload or
credential leaves the cluster. Use a relative, private state directory:

```powershell
python .github\scripts\preserve-grafana-state.py plan --state-directory .grafana-preservation-state
python .github\scripts\preserve-grafana-state.py hold --state-directory .grafana-preservation-state --approve-live-writes --hold-tailscale-ingress
python .github\scripts\preserve-grafana-state.py seed --state-directory .grafana-preservation-state --approve-live-writes
```

The source must still be pod UID
`62fb40e7-ab02-46d5-b6fc-0a7b4d05633d` on `rishik-worker2`; a different UID
requires a fresh reviewed source path, not a force flag. The runner checks this
before creating the hostPath Job and after seeding. It also preserves the
Prometheus claim UID `d5436bdf-a643-4028-8a7f-513a52ed66a8` and Grafana's
`grafana-admin-credentials` / `admin-user` / `admin-password` references.

For the final seed-to-rollout window, the runner first requires zero active
sessions. A temporary, highest-priority Traefik IngressRoute shadows only
`grafana.homelab` on `websecure` using the existing `grafana-tls` reference and
a no-endpoint Service. The original main ingress is untouched. This requires
Traefik's existing `allowEmptyServices=true` and a verified HTTPS **503**.
No conflicting Grafana IngressRoute was present during preparation.
**The Tailscale ingress bypasses Traefik**, so the overlay alone cannot prevent
new edits. The explicit `--hold-tailscale-ingress` exception temporarily points
that captured ingress at the empty Service with UID/resourceVersion/spec
preconditions and a resource-level Flux reconcile-disable annotation. Its
original spec/annotation is restored after internal verification; neither the
root Kustomization nor HelmRelease is suspended. Both public health paths must
be blocked before seeding and remain blocked through rollout. Operators must
also refrain from direct service/port-forward API writes during this window.

The preparation workstation cannot resolve the Tailscale MagicDNS hostname.
When its external HTTPS probe is unavailable, the runner instead verifies
the captured ingress UID's ready Tailscale proxy with the **read-only native
`tailscale serve status --json` API**, requiring its runtime forwarder to
point at the expected Service IP. During hold it additionally requires zero
EndpointSlice/legacy endpoints and no Service selector. It never reads the
proxy's Secret-mounted serve-config file. This prevents treating an
unreachable client probe as proof of maintenance. Verify external Tailscale
HTTPS separately from a tailnet-enabled client after release; the ordinary
LAN hostname still requires a real200→503→200 probe.

The seed Job is rootless UID/GID/fsGroup472, no service-account token, no
capabilities, read-only root filesystem and read-only source hostPath of type
Directory. It uses the cached sidecar image pinned by digest, bounded resources
and a ten-minute deadline. Egress is limited to cluster DNS and Alertmanager9093.
The runner injects the destination PVC UID; applying the bare Job is rejected.

Python SQLite's **online backup API** copies from `mode=ro` with a held read
transaction. Destination `integrity_check` must be `ok` and counts must match
that exact snapshot, even if a writer commits concurrently. The helper refuses
any nonempty destination, copies all other files/plugins without substituting
live SQLite WAL/shm/journal files, and keeps a separate private0700/0600
pre-upgrade database plus native silence JSON under `.grafana-preservation`
on the destination PVC. Logs contain only receipt metadata/counts. Failure
keeps the original source and partial destination intact: do not bypass the
overwrite guard, delete either claim, or remove the maintenance hold.
If aborting before upgrade, `abort-hold --approve-live-writes` restores routes
only while the exact original Grafana is healthy and no seed Job is active;
it retains all partial destination/recovery data. Never bypass a failed seed
with an empty replacement claim.

**Only after a successful seed receipt** may the coordinator merge the chart
upgrade/persistence binding. After rollout, while the hold remains:

```powershell
# Needed only when the seed receipt's silenceCount is nonzero:
python .github\scripts\preserve-grafana-state.py restore-silences --state-directory .grafana-preservation-state --approve-live-writes
python .github\scripts\preserve-grafana-state.py verify --state-directory .grafana-preservation-state
python .github\scripts\preserve-grafana-state.py release --state-directory .grafana-preservation-state --approve-live-writes
python .github\scripts\preserve-grafana-state.py cleanup --state-directory .grafana-preservation-state --approve-live-writes
```

Native silence import uses the running Alertmanager image's exact digest and
`amtool silence import`, with the JSON mounted read-only from the PVC and no
payload logging. Verification requires the same PVC UID/binding, saved-object
counts, database health, preference metadata and every previously enabled
plugin ID. Plugin versions are inventoried before/after (bundled versions may
legitimately advance); also check plugin load errors and dashboard rendering.
Perform the Prometheus/Alertmanager checks above before release.

Cleanup is a strict temporary-resource allowlist using Kubernetes
`DeleteOptions.preconditions.uid`; it cannot delete PVCs, application pods,
databases or recovery files. Keep the local state receipt and on-PVC recovery
copy until post-upgrade backups are verified. The phase-A PVC and production
data are never part of cleanup.

### Custom Dashboards

Custom Grafana dashboards are stored as ConfigMaps in `infrastructure/monitoring/custom-dashboards/` with the label `grafana_dashboard: "1"` for automatic discovery by the Grafana sidecar.

#### RKE2 Control Plane Dashboard
File: `configmap-rke2-control-plane.yaml`

Visualizes Kubernetes control plane components:
- **API Server**: Request latency (P50/P90/P99), request rate by verb/code, open connections
- **ETCD**: Database size, leader changes, defrag events, commit rate
- **Controller Manager**: Work queue depth, reconciler duration P99
- **Scheduler**: Scheduling latency, scheduling failures
- **Kubelet**: Pod churn, container failures/restarts, eviction pressure

#### Homelab Hardware Dashboard
File: `configmap-homelab-hardware.yaml`

Monitors node hardware using node-exporter and kubelet metrics:
- **CPU**: Usage percentage, throttling events, thermal temperature
- **GPU (Intel Iris Xe)**: Temperature, utilization
- **Memory**: Working set usage, node allocatable vs used
- **Disk (NVMe)**: IOPS, read/write latency, queue depth, SMART temperature
- **Network**: Packet drops, errors, bandwidth per interface

#### Error Funnel Dashboard
File: `configmap-error-funnel.yaml`

Tracks errors and issues across the cluster for debugging:
- **API Server Errors**: 4xx and 5xx error rates
- **Pod/Container Errors**: CrashLoopBackOff, image pull errors, restart rates
- **Workload Errors**: Unavailable deployment replicas, failed jobs, DaemonSet issues
- **Ingress/Network Errors**: Traefik error responses, endpoints not ready
- **Storage Errors**: PVC pending/lost, Longhorn volume issues
- **Flux GitOps Errors**: Reconciliation failures, stalled resources

#### Node Feature Discovery Dashboard
File: `configmap-nfd-dashboard.yaml`

Monitors Node Feature Discovery (NFD) metrics:
- **Feature Discovery Duration**: Histogram showing p50/p99 discovery duration by node
- **Node Update Rates**: Success and failure rates for node label updates
- **Labels per Node**: Number of labels applied to each node
- **NFD Worker Status**: Health status of NFD worker pods

#### Intel GPU Device Plugin Dashboard
File: `configmap-intel-gpu-dashboard.yaml`

Monitors Intel GPU device plugin and GPU allocation:
- **GPU Allocation Overview**: Total GPUs, allocated, available, and device health
- **GPU Allocation per Node**: Allocatable vs requested GPU resources per node
- **GPU Utilization**: GPU busy percentage and memory usage
- **Device Health Status**: GPU device health and plugin status
- **GPU Resource Requests/Limits**: Table of workloads with GPU requests

## ServiceMonitors

### Node Feature Discovery (NFD)
File: `servicemonitor-node-feature-discovery.yaml`

Monitors NFD master and worker components in `kube-system` namespace:
- **Port**: 8081
- **Path**: `/metrics`
- **Key Metrics**:
  - `nfd_feature_discovery_duration_seconds`: Histogram of feature discovery duration
  - `nfd_node_updates_total`: Counter of successful node label updates
  - `nfd_node_update_failures_total`: Counter of failed node label updates

### Intel GPU Device Plugin
File: `servicemonitor-intel-gpu-plugin.yaml`

Monitors Intel GPU device plugin in `kube-system` namespace:
- **Port**: 8080
- **Path**: `/metrics`
- **Key Metrics**:
  - GPU allocation and capacity metrics
  - Device health status
  - Plugin operational metrics
