# NFS media PV patterns

The `nfs-media` StorageClass targets the NAS export `192.168.1.215:/volume1/media` using NFSv3.

## Static PV -> pre-created NAS subdir

Use this for existing media folders such as `movies/`, `tv/`, and `downloads/`. The migration-plan work will create app-specific PVs for those directories.

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: media-movies
spec:
  capacity:
    storage: 1Ti
  accessModes:
    - ReadWriteMany
  persistentVolumeReclaimPolicy: Retain
  storageClassName: nfs-media
  mountOptions:
    - nfsvers=3
    - hard
    - nolock
    - noatime
    - _netdev
  csi:
    driver: nfs.csi.k8s.io
    volumeHandle: media-movies
    volumeAttributes:
      server: 192.168.1.215
      share: /volume1/media/movies
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: media-movies
  namespace: media
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: nfs-media
  volumeName: media-movies
  resources:
    requests:
      storage: 1Ti
```

## Dynamic provisioning via `nfs-media`

Use this only for new ad-hoc shares. The CSI driver creates a `<namespace>-<pvcname>/` directory at the root of `/volume1/media`.

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: scratch-share
  namespace: media
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: nfs-media
  resources:
    requests:
      storage: 100Gi
```
