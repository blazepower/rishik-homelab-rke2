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

## CI/CD Pipeline

This repository includes a comprehensive CI/CD pipeline that runs on all pull requests and pushes to the master branch. The pipeline ensures code quality, security, and correctness before changes are merged.

### Pipeline Checks

| Check | Description | Tool |
|-------|-------------|------|
| **YAML Lint** | Validates YAML syntax and formatting | [yamllint](https://github.com/adrienverge/yamllint) |
| **Kubernetes Validation** | Validates Kubernetes manifests against schemas | [kubeconform](https://github.com/yannh/kubeconform) |
| **Kustomize Build** | Ensures kustomizations build successfully | [kustomize](https://kustomize.io/) |
| **Secret Detection** | Scans for accidentally committed secrets | [gitleaks](https://github.com/gitleaks/gitleaks) |
| **Security Scan** | Checks Kubernetes security best practices | [kubesec](https://kubesec.io/) |
| **Trivy Scan** | Scans for misconfigurations and vulnerabilities | [Trivy](https://trivy.dev/) |
| **Flux Validation** | Validates Flux GitOps resources | [Flux CLI](https://fluxcd.io/) |

### Configuration Files

- `.github/workflows/ci.yaml` - GitHub Actions workflow definition
- `.yamllint.yaml` - YAML linting configuration
- `.gitleaks.toml` - Secret detection configuration

### Running Checks Locally

You can run the same checks locally before pushing:

```bash
# YAML Lint
pip install yamllint
yamllint -c .yamllint.yaml .

# Kubernetes Validation (with Flux schemas)
mkdir -p /tmp/flux-schemas
curl -sL https://github.com/fluxcd/flux2/releases/latest/download/crd-schemas.tar.gz | tar zxf - -C /tmp/flux-schemas
find apps -name '*.yaml' -type f ! -name 'kustomization.yaml' -exec kubeconform \
  -strict -ignore-missing-schemas \
  -schema-location default \
  -schema-location '/tmp/flux-schemas/{{ .ResourceKind }}{{ .KindSuffix }}.json' \
  {} \;

# Kustomize Build
kustomize build apps --enable-helm > /dev/null
kustomize build infrastructure --enable-helm > /dev/null
kustomize build clusters/production --enable-helm > /dev/null

# Secret Detection
gitleaks detect --config .gitleaks.toml --source . --verbose

# Trivy Config Scan
trivy config . --severity HIGH,CRITICAL
```

### Security Best Practices

The pipeline enforces several security best practices:

1. **No Secrets in Code**: The gitleaks scanner detects accidentally committed secrets, API keys, and credentials
2. **Kubernetes Security**: kubesec validates that workloads follow security best practices (non-root users, read-only filesystems, etc.)
3. **Configuration Scanning**: Trivy scans for misconfigurations in Kubernetes manifests
4. **Valid Manifests**: kubeconform ensures all manifests are valid Kubernetes resources

### Adding Exceptions

If you need to add exceptions for false positives:

- **yamllint**: Add paths to the `ignore` section in `.yamllint.yaml`
- **gitleaks**: Add patterns to the `allowlist` section in `.gitleaks.toml`
- **kubesec**: Node bootstrap scripts are automatically excluded as they require privileged access