# AdGuard Home Helm Chart

A Helm chart for deploying AdGuard Home - a network-wide DNS ad-blocker and privacy protection service.

## Overview

This chart deploys AdGuard Home on a Kubernetes cluster. AdGuard Home acts as a DNS server that blocks ads, trackers, and malicious domains at the network level.

## Prerequisites

- Kubernetes 1.19+
- Helm 3.0+
- Persistent Volume provisioner (e.g., Longhorn)

## Installing the Chart

```bash
helm install adguard-home ./charts/adguard-home --namespace adguard-home --create-namespace
```

## Uninstalling the Chart

```bash
helm uninstall adguard-home --namespace adguard-home
```

Note: PersistentVolumeClaims are retained due to the `helm.sh/resource-policy: keep` annotation.

## Configuration

The following table lists the configurable parameters of the chart and their default values.

| Parameter | Description | Default |
|-----------|-------------|---------|
| `image.repository` | AdGuard Home image repository | `adguard/adguardhome` |
| `image.tag` | AdGuard Home image tag | `v0.107.52` |
| `image.pullPolicy` | Image pull policy | `IfNotPresent` |
| `replicaCount` | Number of replicas | `1` |
| `service.type` | Service type | `ClusterIP` |
| `service.webPort` | Web UI port | `3000` |
| `service.dnsPort` | DNS port | `53` |
| `resources.requests.cpu` | CPU request | `50m` |
| `resources.requests.memory` | Memory request | `128Mi` |
| `resources.limits.cpu` | CPU limit | `200m` |
| `resources.limits.memory` | Memory limit | `256Mi` |
| `podSecurityContext.runAsNonRoot` | Run as non-root user | `false` |
| `securityContext.capabilities.add` | Linux capabilities to add | `[NET_BIND_SERVICE]` |
| `persistence.config.enabled` | Enable config persistence | `true` |
| `persistence.config.size` | Config volume size | `1Gi` |
| `persistence.config.storageClass` | Config storage class | `longhorn` |
| `persistence.config.accessMode` | Config access mode | `ReadWriteOnce` |
| `persistence.work.enabled` | Enable work data persistence | `true` |
| `persistence.work.size` | Work data volume size | `2Gi` |
| `persistence.work.storageClass` | Work data storage class | `longhorn` |
| `persistence.work.accessMode` | Work data access mode | `ReadWriteOnce` |
| `livenessProbe` | Liveness probe configuration | See values.yaml |
| `readinessProbe` | Readiness probe configuration | See values.yaml |

## Security Considerations

### Running as Root

AdGuard Home requires binding to privileged port 53 (DNS). For this reason:
- `podSecurityContext.runAsNonRoot` is set to `false`
- `NET_BIND_SERVICE` capability is granted to allow binding to port 53

This is the standard approach for DNS servers in Kubernetes.

### Network Exposure

By default, services are of type `ClusterIP`. To expose DNS externally:

```yaml
service:
  type: LoadBalancer
```

Or use an Ingress controller for the web UI (see examples in `apps/adguard-home/`).

## Persistence

AdGuard Home uses two persistent volumes:

1. **Config volume** (`/opt/adguardhome/conf`): Stores configuration files
2. **Work volume** (`/opt/adguardhome/work`): Stores query logs and statistics

Both volumes are created with the `helm.sh/resource-policy: keep` annotation, which prevents Helm from deleting them during uninstallation.

### Disabling Persistence

To disable persistence (not recommended for production):

```yaml
persistence:
  config:
    enabled: false
  work:
    enabled: false
```

## Probes

The chart includes health checks:

- **Liveness Probe**: Checks if AdGuard Home is responsive (HTTP GET on port 3000)
- **Readiness Probe**: Checks if AdGuard Home is ready to serve traffic

## Services

The chart creates two services:

1. **Web Service** (`<release-name>-web`): Exposes port 3000 for the web UI
2. **DNS Service** (`<release-name>-dns`): Exposes port 53 (TCP and UDP) for DNS queries

## Example Values

### Basic configuration

```yaml
replicaCount: 1
resources:
  requests:
    cpu: 50m
    memory: 128Mi
  limits:
    cpu: 200m
    memory: 256Mi
persistence:
  config:
    size: 1Gi
  work:
    size: 2Gi
```

### Production configuration with LoadBalancer

```yaml
replicaCount: 1
service:
  type: LoadBalancer
  loadBalancerIP: 192.168.1.100
resources:
  requests:
    cpu: 100m
    memory: 256Mi
  limits:
    cpu: 500m
    memory: 512Mi
persistence:
  config:
    size: 2Gi
  work:
    size: 5Gi
```

## Post-Installation

After installation, access the web UI to complete the initial setup:

1. Configure admin credentials
2. Set upstream DNS servers
3. Configure blocklists
4. (Optional) Set up DHCP server functionality

## Troubleshooting

### DNS not resolving

Check if the DNS service is running:
```bash
kubectl get svc -n adguard-home
kubectl logs -n adguard-home -l app.kubernetes.io/name=adguard-home
```

### Web UI not accessible

Check if the web service and pods are running:
```bash
kubectl get pods -n adguard-home
kubectl get svc -n adguard-home
```

### PVC issues

Check PVC status:
```bash
kubectl get pvc -n adguard-home
```

## License

This Helm chart is provided as-is under the MIT License.

AdGuard Home itself is licensed under GPL-3.0. See [AdGuard Home repository](https://github.com/AdguardTeam/AdGuardHome) for details.
