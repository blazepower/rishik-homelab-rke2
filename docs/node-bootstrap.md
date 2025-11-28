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

### Files

- `infrastructure/node-bootstrap/iscsi/daemonset-install-iscsi.yaml` - DaemonSet and ConfigMap definitions
- `infrastructure/node-bootstrap/iscsi/iscsi-bootstrap.sh` - Bootstrap script reference
- `infrastructure/node-bootstrap/iscsi/kustomization.yaml` - Kustomization for iSCSI resources

### Note

The open-iscsi package is a prerequisite for Longhorn iSCSI-based storage. The bootstrap automation ensures all nodes have the required packages installed automatically when they join the cluster.
