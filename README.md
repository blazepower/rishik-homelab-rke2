# rishik-homelab-rke2

Kubernetes homelab configuration managed with RKE2 and Flux GitOps.

## Flux GitOps

This repository is managed by [Flux](https://fluxcd.io/), a GitOps tool that automatically synchronizes the cluster state with the configuration in this repository.

**How it works:**
1. Flux monitors this Git repository for changes
2. When changes are detected, Flux automatically applies them to the cluster
3. The cluster state is continuously reconciled to match the repository

**Configuration:**
- Git repository: `ssh://git@github.com/blazepower/rishik-homelab-rke2`
- Branch: `master`
- Sync interval: 10 minutes
- Path: `./clusters/production`
- Prune enabled: Resources removed from Git are deleted from the cluster

**Components:**
- `clusters/production/flux-system/gotk-components.yaml` - Flux controllers and CRDs
- `clusters/production/flux-system/gotk-sync.yaml` - GitRepository and Kustomization for self-management
- `clusters/production/kustomization.yaml` - Root kustomization that includes all infrastructure and apps

**Deploying changes:**
Simply commit and push changes to the `master` branch. Flux will automatically detect and apply them within the sync interval.

## Directory Structure

```
clusters/
└── production/
    ├── flux-system/            # Flux GitOps components
    └── kustomization.yaml      # Production cluster configuration
infrastructure/
├── crds/                       # Custom Resource Definitions
├── namespaces/                 # Namespace definitions
├── networking/                 # Network configuration
│   └── traefik/                # Traefik ingress controller
├── node-bootstrap/             # Node bootstrap automation
│   └── iscsi/                  # iSCSI package installation
├── node-config/                # Node labels and configuration
├── rbac/                       # Role-based access control
├── storage/                    # Storage configuration
│   └── longhorn/               # Longhorn distributed storage
├── monitoring/                 # Monitoring stack (Prometheus, Grafana, Alertmanager)
└── kustomization.yaml
apps/
└── plex/                       # Plex media server application
```

## Monitoring

### kube-prometheus-stack

[kube-prometheus-stack](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack) is deployed to provide comprehensive cluster monitoring with Prometheus, Grafana, and Alertmanager.

**Configuration:**
- Deployed via Helm chart from `https://prometheus-community.github.io/helm-charts`
- Installed in the `monitoring` namespace
- Prometheus retention: 7 days
- Scrape interval: 30 seconds
- Grafana exposed via Traefik ingress at `grafana.homelab`
- Alertmanager enabled

**Files:**
- `infrastructure/monitoring/helmrepository-prometheus-community.yaml` - Helm repository source
- `infrastructure/monitoring/helmrelease-kube-prometheus-stack.yaml` - Helm release configuration
- `infrastructure/monitoring/ingress-grafana.yaml` - Ingress for Grafana UI
- `infrastructure/monitoring/kustomization.yaml` - Kustomization for monitoring resources

**Accessing Grafana:**
Grafana is accessible via the Traefik ingress at http://grafana.homelab. Ensure you have a DNS entry pointing `grafana.homelab` to your Traefik ingress controller's IP address.

Alternatively, use port-forwarding:
```bash
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
```
Then open http://localhost:3000 in your browser.

**Customization:**
To pin a specific chart version, uncomment and set the `version` field in `helmrelease-kube-prometheus-stack.yaml`.

## Storage

### Longhorn

[Longhorn](https://longhorn.io/) is deployed as the default storage class for persistent volumes.

**Configuration:**
- Deployed via Helm chart from `https://charts.longhorn.io`
- Installed in the `storage` namespace
- Set as the default StorageClass
- Data path: `/var/lib/longhorn`
- Default replica count: 1 (increase to 2+ after adding additional nodes)
- Longhorn UI exposed via Traefik ingress at `longhorn.homelab`

**Files:**
- `infrastructure/storage/longhorn/helmrepository-longhorn.yaml` - Helm repository source
- `infrastructure/storage/longhorn/helmrelease-longhorn.yaml` - Helm release configuration
- `infrastructure/storage/longhorn/ingress-longhorn.yaml` - Ingress for Longhorn UI
- `infrastructure/storage/longhorn/kustomization.yaml` - Kustomization for Longhorn resources

**Accessing Longhorn UI:**
Longhorn UI is accessible via the Traefik ingress at http://longhorn.homelab. Ensure you have a DNS entry pointing `longhorn.homelab` to your Traefik ingress controller's IP address.

**Usage:**
Once deployed, Longhorn will be available as the default StorageClass. Create PersistentVolumeClaims without specifying a storageClassName to use Longhorn:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-pvc
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
```

## Networking

### Traefik Ingress Controller

[Traefik](https://traefik.io/) is deployed as the default ingress controller for the cluster, providing HTTPS routing, load-balancing, middleware support, and future Let's Encrypt integration.

**Configuration:**
- Deployed via Helm chart from `https://traefik.github.io/charts`
- Installed in the `kube-system` namespace
- Chart version: 25.0.0
- Deployment type: DaemonSet (runs on all nodes, no external load balancer required)
- Exposes ports 80 (HTTP) and 443 (HTTPS) via hostPorts
- Service type: ClusterIP
- Set as the default IngressClass

**Files:**
- `infrastructure/networking/traefik/helmrepository-traefik.yaml` - Helm repository source
- `infrastructure/networking/traefik/helmrelease-traefik.yaml` - Helm release configuration
- `infrastructure/networking/traefik/kustomization.yaml` - Kustomization for Traefik resources

**Usage:**
Once deployed, Traefik will be available as the default ingress controller. Create Ingress resources to expose your services:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: my-app
  namespace: my-namespace
spec:
  rules:
  - host: myapp.homelab
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: my-service
            port:
              number: 80
```

**Note:** Ensure you have DNS entries pointing your ingress hostnames to your node IPs where Traefik is running.

## Node Bootstrap

### iSCSI Installation

The cluster uses GitOps-managed node bootstrap automation to install open-iscsi on all RKE2 nodes. This is required for Longhorn storage to function properly.

**How it works:**
- A DaemonSet runs on all nodes (including control plane nodes via tolerations)
- The DaemonSet copies a bootstrap script to the host filesystem
- The script installs open-iscsi package and enables the iscsid service

**Configuration:**
- Runs in the `kube-system` namespace
- Uses privileged containers to access the host filesystem
- Bootstrap script is stored in a ConfigMap

**Files:**
- `infrastructure/node-bootstrap/iscsi/daemonset-install-iscsi.yaml` - DaemonSet and ConfigMap definitions
- `infrastructure/node-bootstrap/iscsi/iscsi-bootstrap.sh` - Bootstrap script reference
- `infrastructure/node-bootstrap/iscsi/kustomization.yaml` - Kustomization for iSCSI resources

**Note:** The open-iscsi package is a prerequisite for Longhorn iSCSI-based storage. The bootstrap automation ensures all nodes have the required packages installed automatically when they join the cluster.