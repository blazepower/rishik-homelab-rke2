# Scheduling and Resource Management

This document describes the scheduling and resource management components in the Homelab RKE2 cluster.

## Table of Contents

- [Priority Classes](#priority-classes)
- [Descheduler](#descheduler)
- [KEDA (Kubernetes Event-Driven Autoscaling)](#keda-kubernetes-event-driven-autoscaling)
- [Resource Quotas and Limit Ranges](#resource-quotas-and-limit-ranges)
- [Applying to New Namespaces](#applying-to-new-namespaces)
- [Pod Disruption Budgets](#pod-disruption-budgets)

## Priority Classes

Priority classes ensure that critical workloads are not evicted when the cluster is under resource pressure. The following priority classes are defined:

| Priority Class | Value | Global Default | Preemption | Description |
|----------------|-------|----------------|------------|-------------|
| `system-critical` | 1,000,000 | No | PreemptLowerPriority | Critical system components (ingress, storage, networking). Should not be evicted. |
| `infrastructure-high` | 100,000 | No | PreemptLowerPriority | Important infrastructure components (monitoring, logging, cert-manager). May preempt application workloads. |
| `application-default` | 1,000 | Yes | PreemptLowerPriority | Default priority for application workloads. |
| `application-low` | 100 | No | Never | Low priority for batch jobs and non-critical workloads. Will not preempt other pods. |

### When to Use Each Priority Class

#### system-critical (1,000,000)

Use for components that are absolutely essential for cluster functionality:

- Traefik (ingress controller)
- Longhorn (storage)
- MetalLB (load balancer)
- CoreDNS
- CNI components

Example usage in a HelmRelease:

```yaml
values:
  priorityClassName: system-critical
```

#### infrastructure-high (100,000)

Use for important infrastructure that supports operations but isn't critical for basic cluster functionality:

- Prometheus / Grafana / Alertmanager
- Loki / Promtail
- cert-manager
- Descheduler

Example usage:

```yaml
values:
  priorityClassName: infrastructure-high
```

#### application-default (1,000)

This is the global default. All pods without an explicit `priorityClassName` will use this priority. Suitable for:

- Regular application workloads
- APIs and services
- Databases

No explicit configuration needed as this is the default.

#### application-low (100)

Use for workloads that can be interrupted:

- Batch jobs
- Scheduled tasks
- Development/testing workloads
- Non-critical background processes

Example usage:

```yaml
spec:
  template:
    spec:
      priorityClassName: application-low
```

### Component Priority Assignments

The following infrastructure components have been configured with priority classes:

| Component | Priority Class |
|-----------|----------------|
| Traefik | system-critical |
| MetalLB (controller & speaker) | system-critical |
| Longhorn (manager & driver) | system-critical |
| Prometheus | infrastructure-high |
| Grafana | infrastructure-high |
| Alertmanager | infrastructure-high |
| Loki | infrastructure-high |
| Promtail | infrastructure-high |
| cert-manager | infrastructure-high |
| Descheduler | infrastructure-high |

## Descheduler

The descheduler runs every hour to rebalance pods across nodes for better resource utilization.

### Location

```
infrastructure/scheduling/descheduler/
├── helmrepository-descheduler.yaml
├── helmrelease-descheduler.yaml
└── kustomization.yaml
```

### Configuration

The descheduler is configured with the following strategies:

#### Balance Strategies

- **RemoveDuplicates**: Removes duplicate pods from the same node (excluding DaemonSets).
- **LowNodeUtilization**: Moves pods from overutilized nodes to underutilized ones.
  - Thresholds (node considered underutilized below): CPU 20%, Memory 20%, Pods 20%
  - Target thresholds (stop moving when above): CPU 50%, Memory 50%, Pods 50%

#### Deschedule Strategies

- **RemovePodsViolatingNodeAffinity**: Removes pods that violate required node affinity rules.
- **RemovePodsViolatingInterPodAntiAffinity**: Removes pods that violate inter-pod anti-affinity rules.
- **RemovePodsViolatingNodeTaints**: Removes pods that violate node taints.

### Safety Features

- Does not evict pods with local storage
- Does not evict system-critical pods
- Evicts failed bare pods
- Ensures pods can be scheduled elsewhere before evicting (nodeFit: true)

### Resource Usage

```yaml
resources:
  requests:
    cpu: "25m"
    memory: "64Mi"
  limits:
    cpu: "100m"
    memory: "128Mi"
```

## KEDA (Kubernetes Event-Driven Autoscaling)

[KEDA](https://keda.sh/) enables event-driven autoscaling for Kubernetes workloads, including the ability to scale to zero when idle. This is used to optimize power consumption for low-traffic applications.

### Location

```
infrastructure/scheduling/keda/
├── namespace.yaml
├── helmrepository-keda.yaml
├── helmrelease-keda.yaml
└── kustomization.yaml
```

### Why KEDA?

- **Scale to Zero**: Unlike native HPA, KEDA can scale workloads down to zero replicas
- **Event-Driven**: Scales based on external metrics (Prometheus, HTTP traffic, queues, etc.)
- **Cost Savings**: Reduces resource consumption for infrequently-used applications
- **Stability**: Conservative cooldown periods prevent flapping

### Configured ScaledObjects

The following applications are configured with KEDA for automatic scaling:

#### Prometheus-Based Scaling (HTTP Traffic)

| Application | Cooldown | Trigger |
|-------------|----------|---------|
| `calibre-web` | 10 min | HTTP requests in last 10 min |
| `homebox` | 10 min | HTTP requests in last 10 min |
| `kindle-sender` | 15 min | Pending files metric |

These apps scale to zero when there's no traffic/activity, and scale back to 1 replica when requests arrive.

#### Time-Based Scaling (Cron)

| Application | Active Hours | Timezone |
|-------------|--------------|----------|
| `overseerr` | 8 AM - 1 AM | America/Los_Angeles |
| `bookshelf` | 8 AM - 1 AM | America/Los_Angeles |

These apps are active during typical usage hours and scale to zero overnight.

### ScaledObject Configuration

Example ScaledObject for an HTTP-traffic-based scaler:

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: calibre-web-scaler
  namespace: media
spec:
  scaleTargetRef:
    name: calibre-web
  pollingInterval: 30        # Check every 30 seconds
  cooldownPeriod: 600        # Wait 10 min before scaling to zero
  minReplicaCount: 0
  maxReplicaCount: 1
  fallback:
    failureThreshold: 5
    replicas: 1              # Keep running if metrics fail
  triggers:
    - type: prometheus
      metadata:
        serverAddress: http://prometheus-operated.monitoring:9090
        query: |
          sum(increase(traefik_service_requests_total{service=~"calibre-web.*"}[10m])) or vector(0)
        threshold: "1"
```

Example ScaledObject for time-based scaling:

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: overseerr-scaler
  namespace: media
spec:
  scaleTargetRef:
    name: overseerr
  minReplicaCount: 0
  maxReplicaCount: 1
  fallback:
    failureThreshold: 3
    replicas: 1
  triggers:
    - type: cron
      metadata:
        timezone: America/Los_Angeles
        start: "0 8 * * *"    # 8 AM
        end: "0 1 * * *"      # 1 AM
        desiredReplicas: "1"
```

### Stability Configuration

KEDA is configured with stability-focused settings:

| Setting | Value | Rationale |
|---------|-------|-----------|
| `pollingInterval` | 30-60s | Reduces overhead while remaining responsive |
| `cooldownPeriod` | 600-900s | Prevents rapid scale up/down (flapping) |
| `fallback.replicas` | 1 | Keeps app running if metrics are unavailable |
| `failureThreshold` | 3-5 | Tolerates transient metric issues |

### Resource Usage

KEDA operator uses minimal resources:

```yaml
resources:
  operator:
    requests:
      cpu: "10m"
      memory: "64Mi"
    limits:
      cpu: "100m"
      memory: "256Mi"
  metricServer:
    requests:
      cpu: "10m"
      memory: "64Mi"
    limits:
      cpu: "50m"
      memory: "128Mi"
```

### Adding a New ScaledObject

To add KEDA scaling to an existing application:

1. Create a `scaledobject.yaml` in the app's directory
2. Add it to the app's `kustomization.yaml`
3. Choose an appropriate trigger type:
   - `prometheus` for metric-based scaling
   - `cron` for time-based scaling
4. Set conservative cooldown periods (10+ minutes)
5. Configure fallback replicas for stability

### Monitoring KEDA

KEDA exposes Prometheus metrics for monitoring:

- `keda_scaler_metrics_value` - Current metric value
- `keda_scaled_object_paused` - Whether scaling is paused
- `keda_resource_totals` - Total scaled resources

## Resource Quotas and Limit Ranges

### Limit Ranges

Default limit ranges ensure that containers have sensible resource requests/limits if not specified.

#### Default LimitRange

Applied to the `default` namespace as a template:

```yaml
spec:
  limits:
    - default:
        cpu: "500m"
        memory: "256Mi"
      defaultRequest:
        cpu: "50m"
        memory: "64Mi"
      max:
        cpu: "2"
        memory: "2Gi"
      min:
        cpu: "10m"
        memory: "16Mi"
      type: Container
```

### Resource Quotas

Resource quotas limit the total resources that can be consumed in a namespace.

#### Template ResourceQuota

Located at `infrastructure/policies/resource-defaults/resource-quota-template.yaml`:

```yaml
spec:
  hard:
    requests.cpu: "4"
    requests.memory: "8Gi"
    limits.cpu: "8"
    limits.memory: "16Gi"
    pods: "20"
    persistentvolumeclaims: "10"
    services: "10"
    secrets: "20"
    configmaps: "20"
```

## Applying to New Namespaces

### Adding a LimitRange to a New Namespace

1. Copy the template from `infrastructure/policies/resource-defaults/limit-range-default.yaml`
2. Update the namespace in the metadata
3. Adjust the limits as needed for your namespace
4. Add to the appropriate kustomization.yaml

Example for a new application namespace:

```yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: default-limits
  namespace: my-app
spec:
  limits:
    - default:
        cpu: "250m"
        memory: "128Mi"
      defaultRequest:
        cpu: "25m"
        memory: "32Mi"
      max:
        cpu: "1"
        memory: "1Gi"
      min:
        cpu: "10m"
        memory: "16Mi"
      type: Container
```

### Adding a ResourceQuota to a New Namespace

1. Copy the template from `infrastructure/policies/resource-defaults/resource-quota-template.yaml`
2. Update the namespace in the metadata
3. Adjust the quotas based on your namespace's expected usage
4. Add to the appropriate kustomization.yaml

Example for a production application namespace:

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: namespace-quota
  namespace: production
spec:
  hard:
    requests.cpu: "8"
    requests.memory: "16Gi"
    limits.cpu: "16"
    limits.memory: "32Gi"
    pods: "50"
    persistentvolumeclaims: "20"
    services: "20"
    secrets: "50"
    configmaps: "50"
```

### Best Practices

1. **Always set resource requests and limits** on your pods to ensure proper scheduling and resource allocation.

2. **Use appropriate priority classes** to ensure critical workloads survive resource pressure.

3. **Monitor resource usage** via Grafana dashboards to adjust quotas and limits as needed.

4. **Start conservative** with quotas and increase as needed rather than starting too high.

5. **Consider namespace isolation** - each team or application should have its own namespace with appropriate quotas.

## Pod Disruption Budgets

PodDisruptionBudgets (PDBs) ensure workload availability during voluntary disruptions such as:
- Node drains for maintenance
- Cluster upgrades
- Node scaling operations

All infrastructure workloads have PDBs configured with `minAvailable: 50%`.

### Configured PDBs

| Component | Namespace | Min Available |
|-----------|-----------|---------------|
| Traefik | kube-system | 50% |
| Longhorn Manager | storage | 50% |
| Longhorn CSI Plugin | storage | 50% |
| MetalLB Controller | metallb-system | 50% |
| MetalLB Speaker | metallb-system | 50% |
| Prometheus | monitoring | 50% |
| Alertmanager | monitoring | 50% |
| Grafana | monitoring | 50% |
| Loki | monitoring | 50% |
| Promtail | monitoring | 50% |
| cert-manager | cert-manager | 50% |
| cert-manager Webhook | cert-manager | 50% |
| cert-manager CA Injector | cert-manager | 50% |
| Sealed Secrets | flux-system | 50% |
| Kyverno Admission | kyverno | 50% |
| Kyverno Background | kyverno | 50% |
| Flux Source Controller | flux-system | 50% |
| Flux Kustomize Controller | flux-system | 50% |
| Flux Helm Controller | flux-system | 50% |
| Flux Notification Controller | flux-system | 50% |

### Verifying PDBs

```bash
# List all PDBs
kubectl get pdb -A

# Check PDB status
kubectl describe pdb <pdb-name> -n <namespace>
```
