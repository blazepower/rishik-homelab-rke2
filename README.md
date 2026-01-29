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
│   ├── kyverno-install/        # Kyverno installation
│   ├── kyverno-policies/       # ClusterPolicy resources
│   ├── network-policies/       # Kubernetes NetworkPolicy resources
│   └── rwx-access/             # RBAC for RWX storage management
├── rbac/                       # Role-based access control
├── storage/                    # Storage configuration (Longhorn)
├── monitoring/                 # Monitoring stack (Prometheus, Grafana, Alertmanager)
├── logging/                    # Logging stack (Loki, Promtail)
├── scheduling/                 # Scheduling components (Descheduler, KEDA)
├── cert-manager/               # TLS certificate management
├── sealed-secrets/             # GitOps-safe secret encryption
└── kustomization.yaml
apps/
├── adguard-home/               # AdGuard Home DNS server
├── bazarr/                     # Bazarr subtitles manager
├── bookshelf/                  # Bookshelf book library manager
├── calibre-web/                # Calibre-Web eBook library browser
├── hardcover-sync/             # Hardcover reading list sync (CronJob)
├── homebox/                    # Homebox inventory management
├── homepage/                   # Homepage dashboard
├── kaneo/                      # Kaneo project management
├── kindle-sender/              # Kindle eBook delivery service
├── overseerr/                  # Overseerr media request management
├── paperless-ngx/              # Paperless-ngx document management
├── plex/                       # Plex media server
├── prowlarr/                   # Prowlarr indexer manager
├── qbittorrent/                # qBittorrent download client with VPN
├── radarr/                     # Radarr movies manager
├── rreading-glasses/           # Book metadata service
├── sabnzbd/                    # SABnzbd NZB download client
├── sonarr/                     # Sonarr TV shows manager
├── sure-finance/               # Personal finance management
├── syncthing/                  # Syncthing file synchronization
└── vault/                      # HashiCorp Vault secrets management
docs/                           # Detailed component documentation
```

## Components

| Component | Description | Documentation |
|-----------|-------------|---------------|
| **Monitoring** | Prometheus, Grafana, and Alertmanager with custom dashboards | [docs/monitoring.md](docs/monitoring.md) |
| **Logging** | Loki and Promtail for centralized log aggregation | [docs/logging.md](docs/logging.md) |
| **Storage** | Longhorn distributed storage as default StorageClass | [docs/storage.md](docs/storage.md) |
| **Networking** | Traefik ingress controller and MetalLB load balancer | [docs/networking.md](docs/networking.md) |
| **TLS** | cert-manager for TLS certificate management | [docs/tls.md](docs/tls.md) |
| **Sealed Secrets** | Bitnami Sealed Secrets for GitOps-safe secret management | [docs/sealed-secrets.md](docs/sealed-secrets.md) |
| **Policies** | Kyverno policy engine for admission control | [docs/policies.md](docs/policies.md) |
| **Scheduling** | Descheduler and KEDA for workload optimization | [docs/scheduling.md](docs/scheduling.md) |
| **Node Bootstrap** | Automated iSCSI and GPU bootstrap | [docs/node-bootstrap.md](docs/node-bootstrap.md) |
| **GPU Acceleration** | Intel QuickSync hardware transcoding | [infrastructure/accelerators/intel-gpu/](infrastructure/accelerators/intel-gpu/) |
| **CI/CD** | Validation and security scanning pipeline | [docs/ci-cd.md](docs/ci-cd.md) |

## Power Efficiency

This cluster implements power-saving features to optimize resource usage:

### KEDA Scale-to-Zero

[KEDA](https://keda.sh/) (Kubernetes Event-Driven Autoscaling) enables automatic scaling based on demand, including scaling to zero when idle.

| App | Scaling Strategy | Behavior |
|-----|------------------|----------|
| `calibre-web` | Prometheus (HTTP traffic) | Scales to 0 after 10 min idle |
| `homebox` | Prometheus (HTTP traffic) | Scales to 0 after 10 min idle |
| `kindle-sender` | Prometheus (pending files) | Scales to 0 after 15 min idle |
| `overseerr` | Time-based (cron) | Active 8 AM - 1 AM only |
| `bookshelf` | Time-based (cron) | Active 8 AM - 1 AM only |

### CronJob Workloads

Periodic workloads run as CronJobs instead of always-on Deployments:

| App | Schedule | Description |
|-----|----------|-------------|
| `hardcover-sync` | Every 2 hours | Syncs reading list with Bookshelf |

### Resource Optimization

- All components have explicit resource requests and limits
- Descheduler runs hourly to rebalance workloads
- Priority classes ensure critical workloads survive resource pressure

See [docs/scheduling.md](docs/scheduling.md) for detailed configuration.

## Applications

### Media Stack

| Application | Port | Description |
|-------------|------|-------------|
| **Plex** | 32400 | Media server with Intel QuickSync GPU transcoding |
| **Prowlarr** | 9696 | Indexer manager |
| **Sonarr** | 8989 | TV shows manager |
| **Radarr** | 7878 | Movies manager |
| **SABnzbd** | 8080 | NZB download client |
| **qBittorrent** | 8080 | BitTorrent client with VPN |
| **Bazarr** | 6767 | Subtitles manager |
| **Overseerr** | 5055 | Media request management |

### Book Management

| Application | Port | Description |
|-------------|------|-------------|
| **Bookshelf** | 8787 | Book library manager |
| **Calibre-Web** | 8083 | eBook library browser |
| **rreading-glasses** | 8788 | Book metadata service |
| **Kindle Sender** | - | Automatic eBook delivery |
| **Hardcover Sync** | - | Reading list synchronization |

### Productivity & Utilities

| Application | Port | Description |
|-------------|------|-------------|
| **Homepage** | 3000 | Dashboard |
| **Paperless-ngx** | 8000 | Document management |
| **Homebox** | 7745 | Inventory management |
| **Kaneo** | 80/1337 | Project management |
| **Sure-Finance** | 3000 | Personal finance |
| **Syncthing** | 8384 | File synchronization |
| **Vault** | 8200 | Secrets management |
| **AdGuard Home** | 3000 | DNS server and ad blocker |

### Shared Configuration

All apps in the media namespace share common configuration:

| Setting | Value |
|---------|-------|
| **Namespace** | `media` |
| **Node** | `rishik-worker1` (pinned via nodeSelector) |
| **Storage Class** | `longhorn` |
| **Timezone** | America/Los_Angeles |
| **Media Mount** | `/media/rishik/Expansion` → `/media` |
| **Ingress** | `<app>.homelab` with TLS |

## Performance Tuning

This cluster is optimized for a resource-constrained homelab environment:

### Resource Limits

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Prometheus | 100m | 500m | 256Mi | 1Gi |
| Grafana | 50m | 200m | 128Mi | 256Mi |
| Alertmanager | 25m | 100m | 64Mi | 128Mi |
| Loki | 100m | 500m | 256Mi | 512Mi |
| Traefik | 50m | 200m | 64Mi | 128Mi |
| KEDA Operator | 10m | 100m | 64Mi | 256Mi |

### Monitoring Optimizations

- **Scrape interval**: 60s (reduced from default 30s)
- **Retention**: 7 days for Prometheus metrics
- **Disabled components**: kubeControllerManager, kubeScheduler, kubeProxy, kubeEtcd (RKE2 managed)

### Storage Optimizations

- **Replica count**: 1 (single replica for homelab)
- **Replica auto-balance**: Disabled to reduce background I/O
- **Orphan auto-deletion**: Enabled

## Dependency Management

This repository uses [Dependabot](https://docs.github.com/en/code-security/dependabot) for GitHub Actions updates.

- **Update schedule:** Weekly on Mondays at 06:00 UTC
- **Configuration:** `.github/dependabot.yml`

**Note:** Helm chart updates are managed via Flux HelmReleases, not Dependabot.
