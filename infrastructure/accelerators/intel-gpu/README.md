# Intel QuickSync GPU Acceleration

This module enables Intel QuickSync hardware transcoding on the RKE2 cluster using the Intel GPU Device Plugin.

## Prerequisites

Before the GPU device plugin can function, you must prepare the nodes with Intel iGPU hardware.

### 1. Install Required Kernel Modules

On each node with an Intel GPU, ensure the following kernel modules are loaded:

```bash
# Load modules (run as root)
modprobe i915
modprobe drm
modprobe drm_kms_helper

# Make persistent across reboots
cat <<EOF | sudo tee /etc/modules-load.d/intel-gpu.conf
i915
drm
drm_kms_helper
EOF
```

### 2. Install VAAPI and MESA Packages

Install the required packages for hardware video acceleration:

```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y \
  intel-media-va-driver \
  vainfo \
  mesa-va-drivers \
  intel-gpu-tools

# Verify VAAPI installation
vainfo
```

### 3. Verify /dev/dri Device Nodes

Confirm that the GPU device nodes are present:

```bash
ls -la /dev/dri/

# Expected output should include:
# - renderD128 (render node)
# - card0 (DRM device)
```

### 4. Label Nodes for GPU Scheduling

Label nodes that have Intel GPUs to enable the device plugin scheduling:

```bash
kubectl label node <node-name> intel.feature.node=true
```

Example for homelab nodes:

```bash
kubectl label node rishik-controller intel.feature.node=true
kubectl label node rishik-worker1 intel.feature.node=true
```

## Deployed Resources

This module deploys:

| Resource | Type | Namespace | Description |
|----------|------|-----------|-------------|
| intel | HelmRepository | flux-system | Intel Helm Charts repository |
| nfd | HelmRepository | flux-system | Node Feature Discovery Helm Charts repository |
| node-feature-discovery | HelmRelease | kube-system | Node Feature Discovery (provides NodeFeatureRule CRD) |
| intel-device-plugins-operator | HelmRelease | kube-system | Intel Device Plugins Operator (provides GpuDevicePlugin CRD) |
| intel-device-plugin-gpu | HelmRelease | kube-system | Intel GPU Device Plugin DaemonSet |

### Dependencies

The Intel GPU Device Plugin requires the following prerequisites to be installed first:

1. **Node Feature Discovery (NFD)** - Provides the `NodeFeatureRule` CRD used for node labeling
2. **Intel Device Plugins Operator** - Provides the `GpuDevicePlugin` CRD and operator functionality

These dependencies are managed automatically via FluxCD's `dependsOn` configuration.

### Configuration

The GPU device plugin is configured with:

- **sharedDevNum: 4** - Allows up to 4 containers to share each GPU device
- **nodeSelector**: Only runs on nodes labeled with `intel.feature.node=true`

## Using GPU in Workloads

### Adding GPU Limits to Plex

To enable hardware transcoding in Plex, add GPU resource limits to the Plex deployment:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: plex
  namespace: media
spec:
  template:
    spec:
      containers:
        - name: plex
          resources:
            limits:
              gpu.intel.com/i915: "1"
```

**Note:** The Plex container also needs the `PLEX_HW_TRANSCODE=1` environment variable or appropriate Plex settings enabled to use hardware transcoding.

### Generic Workload Example

For any workload requiring Intel GPU access:

```yaml
resources:
  limits:
    gpu.intel.com/i915: "1"
```

## Validation

### Verify GPU Resource is Available

After the device plugin is running, verify that the GPU resource appears in node capacity:

```bash
kubectl describe node <node-name> | grep -A5 "Capacity:"
```

Look for:

```
Capacity:
  cpu:                   8
  memory:                32Gi
  gpu.intel.com/i915:    1
```

### Verify Device Plugin Pod Status

```bash
kubectl get pods -n kube-system -l app.kubernetes.io/name=intel-device-plugins-gpu
```

### Check Device Plugin Logs

```bash
kubectl logs -n kube-system -l app.kubernetes.io/name=intel-device-plugins-gpu
```

## Troubleshooting

### GPU Resource Not Appearing

1. Verify the node is labeled: `kubectl get nodes --show-labels | grep intel.feature.node`
2. Check if /dev/dri exists on the host node
3. Verify kernel modules are loaded: `lsmod | grep i915`
4. Check device plugin pod logs for errors

### Permission Denied Errors

Ensure the device plugin has access to /dev/dri:

```bash
ls -la /dev/dri/
# renderD128 should be readable by the container group
```

### Hardware Transcoding Not Working in Plex

1. Verify GPU is allocated to Plex pod: `kubectl describe pod <plex-pod> -n media | grep gpu`
2. Check Plex transcoding logs for hardware encoder availability
3. Ensure Plex Pass subscription is active (required for hardware transcoding)

## References

- [Intel Device Plugins for Kubernetes](https://github.com/intel/intel-device-plugins-for-kubernetes)
- [Intel GPU Device Plugin](https://intel.github.io/intel-device-plugins-for-kubernetes/cmd/gpu_plugin/README.html)
- [Plex Hardware Transcoding](https://support.plex.tv/articles/115002178853-using-hardware-accelerated-streaming/)
