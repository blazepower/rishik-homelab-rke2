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
- Blacklists the `simpledrm` built-in driver via `initcall_blacklist` kernel boot parameter (prevents conflicts with Intel GPU device plugin)
- Creates kernel module configuration files on the host
- Loads i915, drm, drm_kms_helper, and video kernel modules
- Installs Intel GPU packages via apt-get in a chroot environment
- Uses an idempotency marker (`/var/lib/gpu-bootstrap-v4.done`) to prevent re-running

### simpledrm Driver Blacklisting

The `simpledrm` driver is blacklisted via the `initcall_blacklist=simpledrm_platform_driver_init` kernel boot parameter in `/etc/default/grub`. This is necessary because simpledrm can interfere with the Intel GPU device plugin by claiming the GPU device before the i915 driver loads.

**Note**: The `simpledrm` driver is built into the kernel, not a loadable module. This means `module_blacklist` or `/etc/modprobe.d/` blacklist files will not work. The `initcall_blacklist` kernel parameter prevents the driver's init function from being called during boot, which is the correct approach for built-in drivers.

#### Why GRUB boot parameter instead of modprobe.d?

Using a kernel boot parameter provides these advantages:

- **Works for built-in drivers**: Since simpledrm is compiled into the kernel, only `initcall_blacklist` can prevent it from initializing
- **Easier to override temporarily**: At the GRUB menu, press `e` to edit, remove the parameter, and boot
- **More visible/discoverable**: Easier to find and understand than files scattered in `/etc/modprobe.d/`
- **No initramfs update required**: Takes effect on next boot without needing to regenerate initramfs

#### Temporarily Re-enabling simpledrm

If you need to temporarily re-enable simpledrm for troubleshooting (e.g., early boot console issues), you can do so at boot time without modifying any files:

1. Reboot the node
2. When the GRUB menu appears, press `e` to edit the boot entry
3. Find the line starting with `linux` and locate `initcall_blacklist=simpledrm_platform_driver_init`
4. Delete `initcall_blacklist=simpledrm_platform_driver_init` from the line
5. Press `Ctrl+X` or `F10` to boot with the modified parameters

This change is temporary and only affects the current boot. The blacklist will be restored on the next normal reboot.

#### Permanently Re-enabling simpledrm

To permanently re-enable simpledrm (not recommended unless you understand the implications for GPU device plugin):

```bash
# Edit GRUB configuration
sudo nano /etc/default/grub

# Remove "initcall_blacklist=simpledrm_platform_driver_init" from GRUB_CMDLINE_LINUX_DEFAULT

# Update GRUB
sudo update-grub

# Reboot
sudo reboot
```

**Note**: Permanently removing the blacklist may cause the Intel GPU device plugin to fail to detect the GPU. Only do this if you're troubleshooting specific issues and understand the trade-offs.

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
