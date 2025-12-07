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
├── paperless-ngx/              # Paperless-ngx document management system
└── plex/                       # Plex media server application
├── prowlarr/                   # Prowlarr indexer manager
├── sonarr/                     # Sonarr TV shows manager
├── radarr/                     # Radarr movies manager
├── sabnzbd/                    # SABnzbd NZB download client
├── bazarr/                     # Bazarr subtitles manager
└── overseerr/                  # Overseerr media request management
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
| **Paperless-ngx** | Document management system for digitizing and organizing paper documents | [apps/paperless-ngx/README.md](apps/paperless-ngx/README.md) |
| **Plex** | Plex Media Server via official Helm chart with Intel QuickSync GPU transcoding | [apps/plex/](#plex-media-server) |
| **Media Automation** | Complete *arr stack for media automation (Prowlarr, Sonarr, Radarr, SABnzbd, Bazarr, Overseerr) | [apps/](#media-automation-arr-stack) |

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
- `infrastructure/crds/kaneo-git-repo.yaml` - GitRepository for Kaneo chart

### Security Notes

The initial deployment includes placeholder secrets for:
- PostgreSQL password (`kaneo_password`)
- JWT access secret (`change_me_to_secure_secret`)

**TODO**: Replace these with SealedSecrets for production use.
## Media Automation (*arr Stack)

The complete *arr stack for media automation is deployed in the `media` namespace. All applications use the [bjw-s app-template Helm chart](https://github.com/bjw-s/helm-charts/tree/main/charts/other/app-template) with linuxserver.io container images.

### Deployed Applications

| Application | Port | Description | Migration |
|-------------|------|-------------|-----------|
| **Prowlarr** | 9696 | Indexer manager - central hub for managing indexers | Restore from backup |
| **Sonarr** | 8989 | TV shows manager - automated TV show downloads | Restore from backup |
| **Radarr** | 7878 | Movies manager - automated movie downloads | Restore from backup |
| **SABnzbd** | 8080 | NZB download client - downloads from Usenet | Restore from backup |
| **Bazarr** | 6767 | Subtitles manager - automated subtitle downloads | New setup |
| **Overseerr** | 5055 | Media request management - user-friendly request interface | New setup |

### Shared Configuration

All apps in the *arr stack share common configuration:

| Setting | Value |
|---------|-------|
| **Namespace** | `media` |
| **Node** | `rishik-worker1` (pinned via nodeSelector) |
| **Storage Class** | `longhorn` |
| **Config PVC Size** | 1Gi per app |
| **User/Group** | PUID=1000, PGID=1000 |
| **Timezone** | America/Los_Angeles |
| **Media Mount** | `/media/rishik/Expansion` → `/media` (hostPath) |
| **Service Type** | ClusterIP (internal only) |
| **Ingress** | `<app>.homelab` (e.g., `prowlarr.homelab`) |
| **TLS** | Enabled via cert-manager with `cluster-ca` ClusterIssuer |

### Resource Allocation

#### Standard Apps (Prowlarr, Sonarr, Radarr, Bazarr, Overseerr)
- **CPU Request**: 100m
- **CPU Limit**: 1000m (1 core)
- **Memory Request**: 256Mi
- **Memory Limit**: 1Gi

#### SABnzbd (Higher for unpacking)
- **CPU Request**: 200m
- **CPU Limit**: 2000m (2 cores)
- **Memory Request**: 512Mi
- **Memory Limit**: 2Gi

### Directory Structure

Download directories on the media mount:
- `/media/downloads/complete/tv/` - Completed TV downloads
- `/media/downloads/complete/movies/` - Completed movie downloads
- `/media/downloads/incomplete/` - In-progress downloads

### Access

All apps are accessible via HTTPS ingress:
- **Prowlarr**: `https://prowlarr.homelab`
- **Sonarr**: `https://sonarr.homelab`
- **Radarr**: `https://radarr.homelab`
- **SABnzbd**: `https://sabnzbd.homelab`
- **Bazarr**: `https://bazarr.homelab`
- **Overseerr**: `https://overseerr.homelab`

### Files Per App

Each app follows the same structure under `apps/<app>/`:
- `helmrelease.yaml` - HelmRelease using bjw-s app-template chart
- `pvc.yaml` - 1Gi Longhorn PVC for config storage
- `configmap.yaml` - Environment variables (PUID, PGID, TZ)
- `ingress.yaml` - Traefik ingress for `<app>.homelab`
- `certificate.yaml` - TLS certificate via cert-manager
- `kustomization.yaml` - Kustomize configuration

### Helm Repository

- `infrastructure/crds/bjw-s-helm-repo.yaml` - HelmRepository for bjw-s charts
