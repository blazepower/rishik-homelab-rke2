# rishik-homelab-rke2

Kubernetes homelab configuration managed with RKE2 and Flux GitOps.

## Flux GitOps

This repository is managed by [Flux](https://fluxcd.io/), a GitOps tool that automatically synchronizes the cluster state with the configuration in this repository.

- Flux monitors this Git repository for changes
- When changes are detected, Flux automatically applies them to the cluster
- The cluster state is continuously reconciled to match the repository

**Deploying changes:** Simply commit and push changes to the `master` branch. Flux will automatically detect and apply them within the sync interval (10 minutes).

## Directory Structure

```
clusters/
└── production/
    ├── flux-system/            # Flux GitOps components
    └── kustomization.yaml      # Production cluster configuration
infrastructure/
├── accelerators/               # Hardware accelerator plugins (Intel GPU)
├── crds/                       # Custom Resource Definitions
├── namespaces/                 # Namespace definitions
├── networking/                 # Network configuration
│   ├── traefik/                # Traefik ingress controller
│   └── metallb/                # MetalLB load balancer
├── node-bootstrap/             # Node bootstrap automation
│   ├── iscsi/                  # iSCSI installation
│   └── gpu/                    # GPU bootstrap DaemonSet for Intel QuickSync
├── node-config/                # Node labels and configuration
├── policies/                   # Kyverno policy engine and cluster policies
│   ├── kyverno-install/        # Kyverno installation (namespace, HelmRelease)
│   ├── kyverno-policies/       # ClusterPolicy resources (applied after Kyverno is ready)
│   │   ├── cluster-policies/   # Custom validation and mutation policies
│   │   └── pss-baseline/       # Pod Security Standards Baseline policies
│   ├── network-policies/       # Kubernetes NetworkPolicy resources
│   └── rwx-access/             # RBAC for RWX storage management
├── rbac/                       # Role-based access control
├── storage/                    # Storage configuration (Longhorn)
├── monitoring/                 # Monitoring stack (Prometheus, Grafana, Alertmanager)
│   ├── dashboards/             # Standard Grafana dashboards
│   └── custom-dashboards/      # Custom dashboards (observability, hardware monitoring)
├── logging/                    # Logging stack (Loki, Promtail)
├── cert-manager/               # TLS certificate management
├── sealed-secrets/             # GitOps-safe secret encryption
└── kustomization.yaml
apps/
├── kaneo/                      # Kaneo project management application
└── plex/                       # Plex media server application
docs/                           # Detailed component documentation
```

## Components

| Component | Description | Documentation |
|-----------|-------------|---------------|
| **Monitoring** | Prometheus, Grafana, and Alertmanager via kube-prometheus-stack with custom dashboards | [docs/monitoring.md](docs/monitoring.md) |
| **Logging** | Loki and Promtail for centralized log aggregation | [docs/logging.md](docs/logging.md) |
| **Storage** | Longhorn distributed storage as default StorageClass | [docs/storage.md](docs/storage.md) |
| **Networking** | Traefik ingress controller and MetalLB load balancer | [docs/networking.md](docs/networking.md) |
| **TLS** | cert-manager for TLS certificate management | [docs/tls.md](docs/tls.md) |
| **Sealed Secrets** | Bitnami Sealed Secrets for GitOps-safe secret management | [docs/sealed-secrets.md](docs/sealed-secrets.md) |
| **Policies** | Kyverno policy engine for admission control and policy enforcement | [docs/policies.md](docs/policies.md) |
| **Node Bootstrap** | Automated iSCSI installation and GPU bootstrap DaemonSet for Intel QuickSync | [docs/node-bootstrap.md](docs/node-bootstrap.md) |
| **GPU Acceleration** | Intel QuickSync hardware transcoding via GPU Device Plugin | [infrastructure/accelerators/intel-gpu/README.md](infrastructure/accelerators/intel-gpu/README.md) |
| **CI/CD** | Comprehensive validation and security scanning pipeline | [docs/ci-cd.md](docs/ci-cd.md) |
| **Kaneo** | Open-source project management tool via official Helm chart | [apps/kaneo/](#kaneo-project-management) |
| **Plex** | Plex Media Server via official Helm chart with Intel QuickSync GPU transcoding | [apps/plex/](#plex-media-server) |

## Dependency Management

This repository uses [Dependabot](https://docs.github.com/en/code-security/dependabot) to automatically check for and create pull requests for GitHub Actions dependency updates.

- **Update schedule:** Weekly on Mondays at 06:00 UTC
- **Configuration:** `.github/dependabot.yml`

**Note:** Helm chart updates are managed via Flux HelmReleases, not Dependabot.

## Performance Tuning

This cluster is optimized for a resource-constrained homelab environment (2-node RKE2 cluster). Key optimizations include:

### Resource Limits

All components have explicit resource requests and limits to prevent runaway resource consumption:

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Prometheus | 100m | 500m | 256Mi | 1Gi |
| Grafana | 50m | 200m | 128Mi | 256Mi |
| Alertmanager | 25m | 100m | 64Mi | 128Mi |
| Loki | 100m | 500m | 256Mi | 512Mi |
| Promtail | 50m | 200m | 64Mi | 128Mi |
| Traefik | 50m | 200m | 64Mi | 128Mi |
| Longhorn Manager | 25m | 200m | 64Mi | 256Mi |
| cert-manager | 25m | 100m | 64Mi | 128Mi |
| MetalLB Controller | 10m | 100m | 32Mi | 128Mi |

### Monitoring Optimizations

- **Scrape interval**: 60s (reduced from default 30s) to lower CPU/memory usage
- **Evaluation interval**: 60s for rule evaluation
- **Retention**: 7 days for Prometheus metrics
- **Disabled components**: kubeControllerManager, kubeScheduler, kubeProxy, kubeEtcd, coreDns monitoring (RKE2 manages these internally)

### Logging Optimizations

- **Log retention**: 14 days (336 hours) for Loki
- **Ingestion limits**: Rate-limited to 4MB/s with 6MB burst
- **Stream limits**: Maximum 5000 streams per user

### Storage Optimizations

- **Replica count**: 1 (single replica for homelab - no HA needed)
- **Replica auto-balance**: Disabled to reduce background I/O
- **Orphan auto-deletion**: Enabled to clean up unused resources
- **Minimal storage threshold**: 15% reserved for safety

### Networking Optimizations

- **Traefik replicas**: 1 (sufficient for homelab traffic)
- **Service type**: LoadBalancer via MetalLB

## Plex Media Server

Plex is deployed using the official [plex-media-server Helm chart](https://github.com/plexinc/pms-docker/tree/master/charts/plex-media-server) from Plex Inc.

### Configuration

| Setting | Value |
|---------|-------|
| **Node** | `rishik-worker1` (pinned via nodeSelector) |
| **Config Storage** | 20Gi Longhorn PVC |
| **Media Path** | `/media/rishik/Expansion` (external HDD, hostPath) |
| **GPU** | Intel QuickSync (`gpu.intel.com/i915: "1"`) for hardware transcoding |
| **LoadBalancer IP** | `192.168.1.200` (MetalLB) |
| **Ingress** | `plex.homelab` (Traefik with TLS) |
| **Port** | 32400 |

### Access

- **Local HTTPS**: `https://plex.homelab` (via Traefik ingress)
- **Remote/Direct**: `http://192.168.1.200:32400` (via LoadBalancer)

### Files

- `apps/plex/helmrelease.yaml` - HelmRelease configuration
- `apps/plex/service.yaml` - LoadBalancer and ClusterIP services
- `apps/plex/ingress.yaml` - Traefik ingress for plex.homelab
- `apps/plex/certificate.yaml` - TLS certificate
- `apps/plex/pvc.yaml` - Longhorn PVC for config storage
- `apps/plex/networkpolicy.yaml` - Network policy for Plex pods
- `infrastructure/crds/plex-helm-repo.yaml` - HelmRepository for Plex chart

## Kaneo Project Management

Kaneo is deployed using the official [Kaneo Helm chart](https://github.com/usekaneo/kaneo) from the Kaneo project.

### Overview

Kaneo is an open-source project management tool with three main components:
- **API** - Backend service (ghcr.io/usekaneo/api) running on port 1337
- **Web** - Frontend service (ghcr.io/usekaneo/web) running on port 80
- **PostgreSQL** - Database (postgres:16-alpine) running on port 5432

### Configuration

| Setting | Value |
|---------|-------|
| **Namespace** | `kaneo` |
| **Database Storage** | 8Gi Longhorn PVC |
| **Ingress** | `kaneo.homelab` (Traefik with TLS) |
| **Path Routing** | `/api/*` → API service, `/*` → Web service |
| **API Port** | 1337 |
| **Web Port** | 80 |

### Resource Limits

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| PostgreSQL | 50m | 200m | 128Mi | 256Mi |
| API | 50m | 200m | 128Mi | 256Mi |
| Web | 25m | 100m | 64Mi | 128Mi |

### Access

- **HTTPS**: `https://kaneo.homelab` (via Traefik ingress with TLS)
- **API Endpoint**: `https://kaneo.homelab/api` (proxied to API service)

### Files

- `apps/kaneo/namespace.yaml` - Namespace definition
- `apps/kaneo/helmrelease.yaml` - HelmRelease configuration
- `apps/kaneo/ingress.yaml` - Traefik ingress with path-based routing
- `apps/kaneo/networkpolicy.yaml` - Network policy for Kaneo pods
- `apps/kaneo/kustomization.yaml` - Kustomize configuration
- `infrastructure/crds/kaneo-helm-repo.yaml` - HelmRepository for Kaneo chart

### Security Notes

The initial deployment includes placeholder secrets for:
- PostgreSQL password (`kaneo_password`)
- JWT access secret (`change_me_to_secure_secret`)

**TODO**: Replace these with SealedSecrets for production use.
