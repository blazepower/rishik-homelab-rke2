# Node Bootstrap

## Node Requirements

This section documents the requirements for nodes joining the cluster.

### Operating System

- Ubuntu 22.04 LTS or 24.04 LTS (Debian-based required for apt-get package installation)
- Other Debian-based distributions may work but are untested

### Hardware Requirements

- **CPU**: x86_64 architecture
- **Memory**: Minimum 4GB RAM recommended
- **Storage**: Sufficient disk space for Longhorn distributed storage

### For GPU/Hardware Transcoding (Optional)

- Intel CPU with integrated graphics (Intel QuickSync Video support)
- Supported Intel GPU generations: 6th gen (Skylake) or newer recommended
- Kernel 5.x or newer for optimal i915 driver support

### Network Requirements

- Nodes must be able to reach the Kubernetes API server
- Nodes must have internet access for package installation during bootstrap
- Nodes should be on the same network segment or have proper routing configured

### Pre-installed Requirements

- None - the bootstrap DaemonSets handle all required package installation

## iSCSI Installation

The cluster uses GitOps-managed node bootstrap automation to install open-iscsi on all RKE2 nodes. This is required for Longhorn storage to function properly.

### How it works

- A DaemonSet runs on all nodes (including control plane nodes via tolerations)
- The DaemonSet copies a bootstrap script to the host filesystem
- The script installs open-iscsi package and enables the iscsid service

### Configuration

- Runs in the `kube-system` namespace
- Uses privileged containers to access the host filesystem
- Bootstrap script is stored in a ConfigMap

### Resource Limits

The bootstrap DaemonSet has resource limits configured to minimize cluster resource usage:

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| installer | 10m         | 50m       | 32Mi           | 64Mi         |

### Health Probes

The bootstrap DaemonSet includes liveness and readiness probes to ensure proper operation:

#### Liveness Probe
- **Type**: Exec command (`cat /tmp/healthy`)
- **Initial delay**: 30 seconds
- **Check interval**: 60 seconds
- **Failure threshold**: 3 attempts
- **Purpose**: Verifies the container is still running after initial setup

#### Readiness Probe
- **Type**: Exec command (checks if bootstrap script exists on host)
- **Initial delay**: 10 seconds
- **Check interval**: 30 seconds
- **Failure threshold**: 3 attempts
- **Purpose**: Confirms the iSCSI bootstrap script has been successfully copied to the host

### Files

- `infrastructure/node-bootstrap/iscsi/daemonset-install-iscsi.yaml` - DaemonSet and ConfigMap definitions
- `infrastructure/node-bootstrap/iscsi/kustomization.yaml` - Kustomization for iSCSI resources

### Bootstrap Script

The bootstrap script (`iscsi-bootstrap.sh`) stored in the ConfigMap performs the following:

1. Checks if open-iscsi is already installed
2. Updates apt package lists
3. Installs the open-iscsi package
4. Enables and starts the iscsid service

### Note

The open-iscsi package is a prerequisite for Longhorn iSCSI-based storage. The bootstrap automation ensures all nodes have the required packages installed automatically when they join the cluster.

### Verifying Bootstrap Status

Check if the DaemonSet is running on all nodes:

```bash
kubectl get daemonset rke2-node-bootstrap-iscsi -n kube-system
```

Check the readiness of bootstrap pods:

```bash
kubectl get pods -n kube-system -l app=rke2-node-bootstrap-iscsi
```

View bootstrap logs:

```bash
kubectl logs -n kube-system -l app=rke2-node-bootstrap-iscsi
```

## GPU Bootstrap (Intel QuickSync)

The GPU bootstrap DaemonSet configures Intel QuickSync hardware transcoding on cluster nodes.

### How it works

- A DaemonSet runs on all nodes with tolerations for all taints
- Creates kernel module configuration files on the host
- Loads i915, drm, drm_kms_helper, and video kernel modules
- Installs Intel GPU packages via apt-get in a chroot environment
- Uses an idempotency marker (`/var/lib/gpu-bootstrap.done`) to prevent re-running

### Packages Installed

- intel-media-va-driver-non-free
- intel-opencl-icd
- libva-dev
- vainfo

### Resource Limits

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| gpu-bootstrap | 10m | 100m | 64Mi | 256Mi |

### Health Probes

- **Liveness**: Checks for idempotency marker file (120s initial delay, 60s period)
- **Readiness**: Checks for idempotency marker file (60s initial delay, 30s period)

### Files

- `infrastructure/node-bootstrap/gpu/gpu-bootstrap-configmap.yaml` - Bootstrap script
- `infrastructure/node-bootstrap/gpu/gpu-bootstrap-daemonset.yaml` - DaemonSet definition
- `infrastructure/node-bootstrap/gpu/kustomization.yaml` - Kustomization

### Verification Commands

```bash
kubectl get daemonset rke2-node-bootstrap-gpu -n kube-system
kubectl get pods -n kube-system -l app=rke2-node-bootstrap-gpu
kubectl logs -n kube-system -l app=rke2-node-bootstrap-gpu
```
