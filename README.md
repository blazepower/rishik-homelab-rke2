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
├── plex/                       # Plex media server application
├── prowlarr/                   # Prowlarr indexer manager
├── sonarr/                     # Sonarr TV shows manager
├── radarr/                     # Radarr movies manager
├── sabnzbd/                    # SABnzbd NZB download client
├── bazarr/                     # Bazarr subtitles manager
├── overseerr/                  # Overseerr media request management
├── bookshelf/                  # Bookshelf book management (Readarr fork)
├── rreading-glasses/           # rreading-glasses metadata backend with PostgreSQL
├── calibre-web/                # Calibre-Web library browser
├── kindle-sender/              # Kindle Sender automatic eBook delivery
└── hardcover-sync/             # Hardcover Sync - sync Want-To-Read list to Bookshelf
docs/                           # Detailed component documentation
```

## Components

| Component | Description | Documentation |
|-----------|-------------|---------------|
| **Monitoring** | Prometheus, Grafana, Alertmanager, and Blackbox Exporter via kube-prometheus-stack with custom dashboards | [docs/monitoring.md](docs/monitoring.md) |
| **Logging** | Loki and Promtail for centralized log aggregation | [docs/logging.md](docs/logging.md) |
| **Storage** | Longhorn distributed storage as default StorageClass | [docs/storage.md](docs/storage.md) |
| **Networking** | Traefik ingress controller and MetalLB load balancer | [docs/networking.md](docs/networking.md) |
| **Tailscale** | Tailscale Operator for secure remote access via tailnet | [docs/networking.md](docs/networking.md#tailscale-operator) |
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
| **Book Management** | Complete book management stack with Bookshelf, rreading-glasses, Calibre-Web, Kindle Sender, and Hardcover Sync | [docs/book-management-stack.md](docs/book-management-stack.md) |

## Dependency Management

This repository uses [Dependabot](https://docs.github.com/en/code-security/dependabot) to automatically check for and create pull requests for GitHub Actions dependency updates.

- **Update schedule:** Weekly on Mondays at 06:00 UTC
- **Configuration:** `.github/dependabot.yml`

**Note:** Helm chart updates are managed via Flux HelmReleases, not Dependabot.

## Tailscale Remote Access

The homelab uses **Tailscale** to provide secure remote access to all services via an encrypted mesh network (tailnet). Tailscale enables access to services from anywhere without exposing them to the public internet.

### Overview

Tailscale is deployed via the official **Tailscale Kubernetes Operator** which manages ingress resources and provides automatic HTTPS with LetsEncrypt certificates for services exposed on the tailnet.

| Setting | Value |
|---------|-------|
| **Namespace** | `tailscale` |
| **Operator Version** | 1.90.9 |
| **Tailnet Domain** | `tail4217c.ts.net` |
| **ProxyGroup** | `homelab-ingress` (2 replicas) |
| **Tags** | `tag:k8s-operator` |

### Service Access via Tailnet

Services with Tailscale ingresses are accessible via HTTPS on the tailnet with automatic LetsEncrypt certificates. Plex is only available on the local network and via its Kubernetes LoadBalancer. Simply connect to your Tailscale network and access services using the URLs below:

#### Media Services

| Service | Local URL | Tailnet URL | Description |
|---------|-----------|-------------|-------------|
| Plex | `https://plex.homelab:32400` | Not exposed via Tailscale (local network / LoadBalancer only) | Media Server |
| Overseerr | `https://overseerr.homelab` | `https://overseerr.tail4217c.ts.net` | Media Request Management |
| Sonarr | `https://sonarr.homelab` | `https://sonarr.tail4217c.ts.net` | TV Shows Manager |
| Radarr | `https://radarr.homelab` | `https://radarr.tail4217c.ts.net` | Movies Manager |
| Bazarr | `https://bazarr.homelab` | `https://bazarr.tail4217c.ts.net` | Subtitles Manager |
| Prowlarr | `https://prowlarr.homelab` | `https://prowlarr.tail4217c.ts.net` | Indexer Manager |
| SABnzbd | `https://sabnzbd.homelab` | `https://sabnzbd.tail4217c.ts.net` | Usenet Downloader |

#### Documents & Books

| Service | Local URL | Tailnet URL | Description |
|---------|-----------|-------------|-------------|
| Paperless-ngx | `https://paperless.homelab` | `https://paperless.tail4217c.ts.net` | Document Management |
| Calibre-Web | `https://calibre.homelab` | `https://calibre.tail4217c.ts.net` | eBook Library Browser |
| Bookshelf | `https://bookshelf.homelab` | `https://bookshelf.tail4217c.ts.net` | Book Manager (Readarr fork) |

#### Productivity

| Service | Local URL | Tailnet URL | Description |
|---------|-----------|-------------|-------------|
| Kaneo | `https://kaneo.homelab` | `https://kaneo.tail4217c.ts.net` | Project Management |
| Homebox | `https://homebox.homelab` | `https://homebox.tail4217c.ts.net` | Inventory Management |
| Sure Finance | `https://sure.homelab` | `https://sure.tail4217c.ts.net` | Finance Manager |
| Syncthing | `https://syncthing.homelab` | Not exposed via Tailscale | File Synchronization |

#### Infrastructure

| Service | Local URL | Tailnet URL | Description |
|---------|-----------|-------------|-------------|
| Grafana | `https://grafana.homelab` | `https://grafana.tail4217c.ts.net` | Monitoring Dashboard |
| Longhorn | `https://longhorn.homelab` | `https://longhorn.tail4217c.ts.net` | Storage Management |
| AdGuard Home | `https://adguard.homelab` | `https://adguard.tail4217c.ts.net` | DNS & Ad Blocking |
| Homepage | `https://home.homelab` | `https://home.tail4217c.ts.net` | Homelab Dashboard |

#### Background Services (No Web UI)

| Service | Description | Access |
|---------|-------------|--------|
| Kindle Sender | Automatic eBook delivery to Kindle | Background service only |
| rreading-glasses | Metadata backend for Bookshelf | Internal cluster service only |
| Hardcover Sync | Syncs Want-To-Read list to Bookshelf | Background service only |

### How It Works

1. **Tailscale Operator** creates ingress proxy pods for each service
2. Each service gets a unique hostname on the tailnet (e.g., `plex.tail4217c.ts.net`)
3. **Automatic HTTPS** via LetsEncrypt certificates provisioned by Tailscale
4. **Zero trust networking** - only devices on your tailnet can access services
5. **No port forwarding** or firewall configuration required

### Configuration Files

- `infrastructure/tailscale/helmrelease.yaml` - Tailscale Operator HelmRelease
- `infrastructure/tailscale/proxygroup.yaml` - ProxyGroup configuration for ingress
- `apps/*/ingress-*-tailscale.yaml` - Individual Tailscale ingress resources
- `infrastructure/monitoring/ingress-grafana-tailscale.yaml` - Grafana Tailscale ingress
- `infrastructure/storage/longhorn/ingress-longhorn-tailscale.yaml` - Longhorn Tailscale ingress

### Security

- All Tailscale ingresses are tagged with `tag:k8s-operator` for ACL management
- Services are only accessible to authenticated devices on the tailnet
- Automatic certificate rotation managed by Tailscale
- Network policies ensure proper pod-to-pod communication

### Benefits

- ✅ **Secure remote access** from anywhere without VPN complexity
- ✅ **Automatic HTTPS** with real LetsEncrypt certificates
- ✅ **No public exposure** - services remain private
- ✅ **Easy setup** - just install Tailscale on your device and connect
- ✅ **Multiple devices** - access from phone, laptop, tablet, etc.

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
| Blackbox Exporter | 25m | 100m | 32Mi | 64Mi |

### Monitoring Optimizations

- **Scrape interval**: 60s (reduced from default 30s) to lower CPU/memory usage
- **Evaluation interval**: 60s for rule evaluation
- **Retention**: 7 days for Prometheus metrics
- **Disabled components**: kubeControllerManager, kubeScheduler, kubeProxy, kubeEtcd, coreDns monitoring (RKE2 manages these internally)
- **Blackbox Exporter**: Enabled for application uptime monitoring with 60s probe interval

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

- **Local HTTPS**: `https://kaneo.homelab` (via Traefik ingress with TLS)
- **API Endpoint**: `https://kaneo.homelab/api` (proxied to API service)
- **Tailnet HTTPS**: `https://kaneo.tail4217c.ts.net` (secure remote access)

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

All apps are accessible via local HTTPS ingress and Tailscale:

**Local Access:**
- **Prowlarr**: `https://prowlarr.homelab`
- **Sonarr**: `https://sonarr.homelab`
- **Radarr**: `https://radarr.homelab`
- **SABnzbd**: `https://sabnzbd.homelab`
- **Bazarr**: `https://bazarr.homelab`
- **Overseerr**: `https://overseerr.homelab`

**Remote Access (Tailnet):**
- **Prowlarr**: `https://prowlarr.tail4217c.ts.net`
- **Sonarr**: `https://sonarr.tail4217c.ts.net`
- **Radarr**: `https://radarr.tail4217c.ts.net`
- **SABnzbd**: `https://sabnzbd.tail4217c.ts.net`
- **Bazarr**: `https://bazarr.tail4217c.ts.net`
- **Overseerr**: `https://overseerr.tail4217c.ts.net`

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

## Book Management Stack

A complete, production-quality book management ecosystem for personal library automation, reading, and Kindle delivery.

### Overview

The book management stack consists of four integrated components:

1. **Bookshelf** - Main book management interface (Readarr fork)
2. **rreading-glasses** - Metadata backend with PostgreSQL
3. **Calibre-Web** - Web-based library browser
4. **Kindle Sender** - Automatic eBook delivery microservice

All components run in the `media` namespace on `rishik-worker1` and share the `/media/rishik/Expansion/books` directory.

### Components

#### Bookshelf

- **Description**: Readarr fork optimized for book management
- **Image**: `ghcr.io/pennydreadful/bookshelf:hardcover-v0.4.10.2765`
- **Chart**: bjw-s app-template v3.5.1
- **Port**: 8787
- **Access**: https://bookshelf.homelab
- **Storage**: 
  - Config: 1Gi Longhorn PVC
  - Media: hostPath `/media/rishik/Expansion` → `/media`
- **Books Path**: `/media/books`
- **Resources**: 100m-1000m CPU, 256Mi-1Gi memory
- **Features**:
  - Book acquisition and organization
  - Metadata enrichment via rreading-glasses
  - Library management and tracking
- **Documentation**: [apps/bookshelf/README.md](apps/bookshelf/README.md)

#### rreading-glasses

- **Description**: Metadata backend service integrating with Hardcover.app API
- **Image**: `docker.io/blampe/rreading-glasses:hardcover`
- **Chart**: bjw-s app-template v3.5.1
- **Port**: 8080 (ClusterIP only, no ingress)
- **Storage**: 
  - PostgreSQL: 8Gi Longhorn PVC
- **Resources**: 50m-200m CPU, 128Mi-256Mi memory
- **Database**: PostgreSQL 16-alpine
  - Database: `rreading_glasses`
  - User: `rreading_user`
- **Features**:
  - Enhanced book metadata from Hardcover.app
  - PostgreSQL-backed caching
  - Internal-only service for Bookshelf
- **Secrets**: HARDCOVER_API_KEY, POSTGRES_PASSWORD, RG_DATABASE_URL
- **Documentation**: [apps/rreading-glasses/README.md](apps/rreading-glasses/README.md)

#### Calibre-Web

- **Description**: Web-based eBook library browser
- **Image**: `lscr.io/linuxserver/calibre-web:0.6.24`
- **Chart**: bjw-s app-template v3.5.1
- **Port**: 8083
- **Access**: https://calibre.homelab
- **Storage**: 
  - Config: 1Gi Longhorn PVC
  - Media: hostPath `/media/rishik/Expansion` → `/media` (READ-ONLY)
- **Calibre Library**: `/media/books/calibre-library`
- **Resources**: 100m-500m CPU, 256Mi-512Mi memory
- **Features**:
  - Online eBook reading (EPUB, PDF, etc.)
  - Book browsing and search
  - Manual email to Kindle
  - User management
  - Metadata editing
- **Secrets**: SMTP credentials for Kindle email delivery
- **Documentation**: [apps/calibre-web/README.md](apps/calibre-web/README.md)

#### Kindle Sender

- **Description**: Custom Go microservice for automatic Kindle delivery
- **Image**: `ghcr.io/blazepower/kindle-sender:v1.0.0`
- **Chart**: bjw-s app-template v3.5.1
- **Storage**: 
  - Data: 100Mi Longhorn PVC (SQLite database)
  - Media: hostPath `/media/rishik/Expansion` → `/media` (READ-ONLY)
- **Watch Path**: `/media/books`
- **Resources**: 25m-100m CPU, 32Mi-64Mi memory
- **Features**:
  - Real-time file watching (fsnotify)
  - Periodic scanning (5 minutes)
  - Duplicate prevention (SQLite tracking)
  - Automatic email delivery to Kindle
  - Support: .epub, .mobi, .azw3, .pdf
  - Max file size: 50MB (configurable)
- **Secrets**: SMTP credentials, KINDLE_EMAIL, SENDER_EMAIL
- **Source Code**: [apps/kindle-sender/src/](apps/kindle-sender/src/)
- **Documentation**: [apps/kindle-sender/README.md](apps/kindle-sender/README.md)

### Security

All components follow strict security practices:

#### Pod Security
- Run as non-root user (UID 1000, GID 1000)
- FSGroup set to 1000 for volume permissions
- All capabilities dropped
- Read-only root filesystem (where possible)
- No privilege escalation

#### Network Policies
- **Bookshelf**: Ingress from Traefik (8787), egress to rreading-glasses (8080) and HTTPS
- **rreading-glasses**: Ingress from Bookshelf only (8080), egress to PostgreSQL (5432) and HTTPS
- **Calibre-Web**: Ingress from Traefik (8083), egress to SMTP and HTTPS
- **Kindle Sender**: No ingress, egress to SMTP and DNS only

#### Secret Management
All sensitive credentials stored as Bitnami SealedSecrets:
- rreading-glasses: Hardcover API key, PostgreSQL credentials
- Calibre-Web: SMTP credentials
- Kindle Sender: SMTP credentials, Kindle email address

### Resource Allocation

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Bookshelf | 100m | 1000m | 256Mi | 1Gi |
| rreading-glasses | 50m | 200m | 128Mi | 256Mi |
| PostgreSQL | 50m | 200m | 128Mi | 256Mi |
| Calibre-Web | 100m | 500m | 256Mi | 512Mi |
| Kindle Sender | 25m | 100m | 32Mi | 64Mi |

### Shared Storage

All components share the media directory:
- **Host Path**: `/media/rishik/Expansion`
- **Container Mount**: `/media`
- **Books Directory**: `/media/books/`
- **Calibre Library**: `/media/books/calibre-library/`

Access modes:
- **Bookshelf**: Read-Write (manages files)
- **Calibre-Web**: Read-Only (browsing only)
- **Kindle Sender**: Read-Only (monitoring only)

### Access URLs

**Local Access:**
- **Bookshelf**: `https://bookshelf.homelab`
- **Calibre-Web**: `https://calibre.homelab`
- **rreading-glasses**: Internal only (no ingress)
- **Kindle Sender**: No web interface (background service)

**Remote Access (Tailnet):**
- **Bookshelf**: `https://bookshelf.tail4217c.ts.net`
- **Calibre-Web**: `https://calibre.tail4217c.ts.net`
- **rreading-glasses**: Internal only
- **Kindle Sender**: Background service only

### Configuration Requirements

#### Before Deployment

1. **Create books directory on host**:
   ```bash
   sudo mkdir -p /media/rishik/Expansion/books
   sudo chown 1000:1000 /media/rishik/Expansion/books
   ```

2. **Initialize Calibre library** (optional):
   ```bash
   # Create directory - Calibre-Web will initialize it on first run
   sudo mkdir -p /media/rishik/Expansion/books/calibre-library
   sudo chown 1000:1000 /media/rishik/Expansion/books/calibre-library
   ```

3. **Seal secrets** for each component:
   - rreading-glasses: Hardcover API key, PostgreSQL password
   - Calibre-Web: SMTP credentials
   - Kindle Sender: SMTP credentials, Kindle email

4. **Configure Kindle email**:
   - Find your Kindle email in Amazon account settings
   - Add sender email to approved list in Amazon

5. **SMTP setup** (Gmail example):
   - Enable 2FA
   - Generate App Password
   - Use in sealed secrets

### Workflow

1. **Acquisition**: Bookshelf downloads and organizes books to `/media/books/`
2. **Metadata**: Bookshelf enriches metadata via rreading-glasses API
3. **Browsing**: Users browse books via Calibre-Web at https://calibre.homelab
4. **Delivery**: Kindle Sender automatically emails new books to Kindle device
5. **Manual Send**: Users can also manually send books via Calibre-Web interface

### Integration

- **Bookshelf ↔ rreading-glasses**: Network policy allows direct connection for metadata
- **Bookshelf → Books**: Writes to `/media/books/` for organization
- **Calibre-Web → Books**: Reads from `/media/books/calibre-library/` for browsing
- **Kindle Sender → Books**: Monitors `/media/books/` for new files

### Files Per Component

Each component follows GitOps best practices:

```
apps/<component>/
├── helmrelease.yaml          # HelmRelease using bjw-s app-template
├── configmap.yaml            # Environment variables
├── pvc.yaml                  # Longhorn PVC (if needed)
├── ingress.yaml              # Traefik ingress (if web UI)
├── certificate.yaml          # TLS certificate (if ingress)
├── networkpolicy.yaml        # Network security policies
├── sealedsecret-*.yaml       # Sealed secrets (if needed)
├── kustomization.yaml        # Kustomize configuration
└── README.md                 # Component documentation
```

Additional for rreading-glasses:
- `postgresql.yaml` - PostgreSQL Deployment, Service, and PVC

Additional for Kindle Sender:
- `src/` - Go application source code
  - `main.go` - Application logic
  - `go.mod` - Go dependencies
  - `Dockerfile` - Multi-stage container build

### Troubleshooting

See individual component READMEs for detailed troubleshooting:
- [Bookshelf README](apps/bookshelf/README.md)
- [rreading-glasses README](apps/rreading-glasses/README.md)
- [Calibre-Web README](apps/calibre-web/README.md)
- [Kindle Sender README](apps/kindle-sender/README.md)

Quick checks:
```bash
# Check all book stack pods
kubectl get pods -n media -l 'app.kubernetes.io/name in (bookshelf,rreading-glasses,calibre-web,kindle-sender)'

# View logs
kubectl logs -n media -l app.kubernetes.io/name=bookshelf -f
kubectl logs -n media -l app.kubernetes.io/name=kindle-sender -f

# Verify media mount
kubectl exec -n media -it <pod-name> -- ls -la /media/books
```

## Application Uptime Monitoring

The homelab uses **Blackbox Exporter** to monitor the availability and performance of all application endpoints via HTTPS probes.

### Overview

Blackbox Exporter is deployed as part of the kube-prometheus-stack and provides comprehensive uptime monitoring for:
- **Media Services**: Plex, Overseerr, Sonarr, Radarr, Bazarr, Prowlarr, SABnzbd
- **Productivity Services**: Paperless-ngx, Calibre-Web, Bookshelf, Kaneo, Homebox, Sure-finance
- **Infrastructure Services**: Grafana, Longhorn

### Configuration

| Setting | Value |
|---------|-------|
| **Namespace** | `monitoring` |
| **Probe Interval** | 60 seconds |
| **Probe Timeout** | 10 seconds |
| **Module** | `http_2xx` (HTTP/HTTPS with valid status codes) |
| **TLS Verification** | Disabled (self-signed certificates) |

> **Security Note:** TLS verification is currently disabled to support self-signed certificates in the homelab environment. For production use, it is recommended to configure Blackbox Exporter to trust your internal CA or use certificate pinning instead of globally disabling TLS verification. This can be done by adding your CA certificate to the Blackbox Exporter configuration.

### Monitored Endpoints

Each application is monitored via a Kubernetes **Probe** resource that configures the Blackbox Exporter to periodically probe the application's HTTPS endpoint.

**Media Services** (`media` namespace):
- `https://plex.homelab:32400`
- `https://overseerr.homelab` (also accessible via `https://overseerr.tail4217c.ts.net`)
- `https://sonarr.homelab` (also accessible via `https://sonarr.tail4217c.ts.net`)
- `https://radarr.homelab` (also accessible via `https://radarr.tail4217c.ts.net`)
- `https://bazarr.homelab` (also accessible via `https://bazarr.tail4217c.ts.net`)
- `https://prowlarr.homelab` (also accessible via `https://prowlarr.tail4217c.ts.net`)
- `https://sabnzbd.homelab` (also accessible via `https://sabnzbd.tail4217c.ts.net`)

**Productivity Services** (`productivity` namespace):
- `https://paperless.homelab` (also accessible via `https://paperless.tail4217c.ts.net`)
- `https://calibre.homelab` (also accessible via `https://calibre.tail4217c.ts.net`)
- `https://bookshelf.homelab` (also accessible via `https://bookshelf.tail4217c.ts.net`)
- `https://kaneo.homelab` (also accessible via `https://kaneo.tail4217c.ts.net`)
- `https://homebox.homelab` (also accessible via `https://homebox.tail4217c.ts.net`)
- `https://sure.homelab` (also accessible via `https://sure.tail4217c.ts.net`)

**Infrastructure Services**:
- `https://grafana.homelab` (`monitoring` namespace) (also accessible via `https://grafana.tail4217c.ts.net`)
- `https://longhorn.homelab` (`storage` namespace) (also accessible via `https://longhorn.tail4217c.ts.net`)

### Grafana Dashboard

A custom Grafana dashboard **"Homelab Uptime Dashboard (Blackbox Exporter)"** provides comprehensive visibility into application health:

#### Features:
1. **Service Status Overview**: Real-time table showing all services with UP/DOWN status (green/red)
2. **Uptime Percentage**: Gauge panels showing uptime % over the selected time range
3. **Response Time Metrics**: Time series graphs of HTTP response latency for each service
4. **SSL Certificate Expiry**: Table showing days until SSL certificate expiration
5. **Probe Duration**: Total probe duration including DNS, connection, TLS handshake, and processing time

#### Metrics Available:
- `probe_success` - Binary indicator (1 = up, 0 = down)
- `probe_http_duration_seconds` - HTTP response time
- `probe_ssl_earliest_cert_expiry` - Unix timestamp of SSL certificate expiry
- `probe_duration_seconds` - Total probe duration

#### Access:
Navigate to Grafana at `https://grafana.homelab` and find the dashboard under:
- **Title**: "Homelab Uptime Dashboard (Blackbox Exporter)"
- **Tags**: `blackbox`, `uptime`, `monitoring`, `homelab`

### Files

All monitoring configuration is located in `infrastructure/monitoring/`:
- `helmrelease-kube-prometheus-stack.yaml` - Blackbox Exporter configuration in HelmRelease values
- `probe-*.yaml` - Individual Probe resources for each application (16 total)
- `custom-dashboards/configmap-blackbox-uptime-dashboard.yaml` - Grafana dashboard ConfigMap

### Resource Usage

Blackbox Exporter is lightweight and optimized for homelab use:
- **CPU Request**: 25m
- **CPU Limit**: 100m
- **Memory Request**: 32Mi
- **Memory Limit**: 64Mi

### Adding New Endpoints

To monitor a new application endpoint:

1. **Create a Probe resource** in `infrastructure/monitoring/probe-<app>.yaml`:
```yaml
apiVersion: monitoring.coreos.com/v1
kind: Probe
metadata:
  name: <app-name>
  namespace: monitoring
  labels:
    app: <app-name>
    release: kube-prometheus-stack
spec:
  jobName: <app-name>
  prober:
    url: kube-prometheus-stack-prometheus-blackbox-exporter.monitoring.svc.cluster.local:9115
  module: http_2xx
  targets:
    staticConfig:
      static:
        - https://<app>.homelab
  interval: 60s
  scrapeTimeout: 10s
```

2. **Add to kustomization.yaml**:
```yaml
resources:
  - probe-<app-name>.yaml
```

3. **Update the dashboard** (optional) to include the new service in visualizations

### Troubleshooting

Check Blackbox Exporter pod status:
```bash
kubectl get pods -n monitoring -l app.kubernetes.io/name=prometheus-blackbox-exporter
```

View Blackbox Exporter logs:
```bash
kubectl logs -n monitoring -l app.kubernetes.io/name=prometheus-blackbox-exporter
```

Query probe metrics directly:
```bash
# Check if a service is up
kubectl exec -n monitoring -it prometheus-kube-prometheus-stack-prometheus-0 -- promtool query instant 'probe_success{job="plex"}'

# Check response time
kubectl exec -n monitoring -it prometheus-kube-prometheus-stack-prometheus-0 -- promtool query instant 'probe_http_duration_seconds{job="plex"}'
```

Verify Probe resources:
```bash
kubectl get probes -n monitoring
kubectl describe probe <probe-name> -n monitoring
```
