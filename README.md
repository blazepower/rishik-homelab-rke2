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
├── crds/                       # Custom Resource Definitions
├── namespaces/                 # Namespace definitions
├── networking/                 # Network configuration (Traefik)
├── node-bootstrap/             # Node bootstrap automation (iSCSI)
├── node-config/                # Node labels and configuration
├── rbac/                       # Role-based access control
├── storage/                    # Storage configuration (Longhorn)
├── monitoring/                 # Monitoring stack (Prometheus, Grafana, Alertmanager)
└── kustomization.yaml
apps/
└── plex/                       # Plex media server application
docs/                           # Detailed component documentation
```

## Components

| Component | Description | Documentation |
|-----------|-------------|---------------|
| **Monitoring** | Prometheus, Grafana, and Alertmanager via kube-prometheus-stack | [docs/monitoring.md](docs/monitoring.md) |
| **Storage** | Longhorn distributed storage as default StorageClass | [docs/storage.md](docs/storage.md) |
| **Networking** | Traefik ingress controller for HTTP/HTTPS routing | [docs/networking.md](docs/networking.md) |
| **Node Bootstrap** | Automated iSCSI installation on cluster nodes | [docs/node-bootstrap.md](docs/node-bootstrap.md) |
| **CI/CD** | Comprehensive validation and security scanning pipeline | [docs/ci-cd.md](docs/ci-cd.md) |

## Dependency Management

This repository uses [Dependabot](https://docs.github.com/en/code-security/dependabot) to automatically check for and create pull requests for GitHub Actions dependency updates.

- **Update schedule:** Weekly on Mondays at 06:00 UTC
- **Configuration:** `.github/dependabot.yml`

**Note:** Helm chart updates are managed via Flux HelmReleases, not Dependabot.