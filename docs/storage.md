# Storage

## Longhorn

[Longhorn](https://longhorn.io/) is deployed as the default storage class for persistent volumes.

### Configuration

- Deployed via Helm chart from `https://charts.longhorn.io`
- Installed in the `storage` namespace
- Set as the default StorageClass
- Data path: `/var/lib/longhorn`
- Default replica count: 1 (increase to 2+ after adding additional nodes)
- Longhorn UI exposed via Traefik ingress at `longhorn.homelab`

### Files

- `infrastructure/storage/longhorn/helmrepository-longhorn.yaml` - Helm repository source
- `infrastructure/storage/longhorn/helmrelease-longhorn.yaml` - Helm release configuration
- `infrastructure/storage/longhorn/ingress-longhorn.yaml` - Ingress for Longhorn UI
- `infrastructure/storage/longhorn/kustomization.yaml` - Kustomization for Longhorn resources

### Accessing Longhorn UI

Longhorn UI is accessible via the Traefik ingress at http://longhorn.homelab. Ensure you have a DNS entry pointing `longhorn.homelab` to your Traefik ingress controller's IP address.

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
