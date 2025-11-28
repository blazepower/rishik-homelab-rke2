# Storage

## Longhorn

[Longhorn](https://longhorn.io/) is deployed as the default storage class for persistent volumes.

### Configuration

- Deployed via Helm chart from `https://charts.longhorn.io`
- Installed in the `storage` namespace
- Set as the default StorageClass
- Data path: `/var/lib/longhorn`
- Default replica count: 1 (increase to 2+ after adding additional nodes)
- Longhorn UI exposed via Traefik ingress at `https://longhorn.homelab`
- Guaranteed instance manager CPU: 5%

### Resource Limits

Longhorn components have resource limits configured:

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Longhorn Manager | 25m | 200m | 64Mi | 256Mi |
| Longhorn Driver | 25m | 200m | 64Mi | 256Mi |
| Longhorn UI | 25m | 100m | 32Mi | 128Mi |

### Files

- `infrastructure/storage/longhorn/helmrepository-longhorn.yaml` - Helm repository source
- `infrastructure/storage/longhorn/helmrelease-longhorn.yaml` - Helm release configuration
- `infrastructure/storage/longhorn/ingress-longhorn.yaml` - Ingress for Longhorn UI (HTTPS)
- `infrastructure/storage/longhorn/kustomization.yaml` - Kustomization for Longhorn resources

### Accessing Longhorn UI

Longhorn UI is accessible via the Traefik ingress at https://longhorn.homelab. Ensure you have:
1. A DNS entry pointing `longhorn.homelab` to your Traefik ingress controller's IP address
2. The homelab CA certificate installed on your device (see [docs/tls.md](tls.md))

Alternatively, use port-forwarding:
```bash
kubectl port-forward -n storage svc/longhorn-frontend 8080:80
```
Then open http://localhost:8080 in your browser.

### Usage

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

### Prerequisites

Longhorn requires open-iscsi to be installed on all cluster nodes. This is handled automatically by the node bootstrap automation. See [docs/node-bootstrap.md](node-bootstrap.md) for details.
