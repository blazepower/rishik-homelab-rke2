# Node Bootstrap

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
