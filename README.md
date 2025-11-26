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
├── node-config/                # Node labels and configuration
├── rbac/                       # Role-based access control
├── storage/                    # Storage configuration
│   └── longhorn/               # Longhorn distributed storage
└── kustomization.yaml
apps/
└── plex/                       # Plex media server application
```

## Storage

### Longhorn

[Longhorn](https://longhorn.io/) is deployed as the default storage class for persistent volumes.

**Configuration:**
- Deployed via Helm chart from `https://charts.longhorn.io`
- Installed in the `storage` namespace
- Set as the default StorageClass
- Data path: `/var/lib/longhorn`
- Default replica count: 1 (increase to 2+ after adding additional nodes)

**Files:**
- `infrastructure/storage/longhorn/helmrepository-longhorn.yaml` - Helm repository source
- `infrastructure/storage/longhorn/helmrelease-longhorn.yaml` - Helm release configuration
- `infrastructure/storage/longhorn/kustomization.yaml` - Kustomization for Longhorn resources

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