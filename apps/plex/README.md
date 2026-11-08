# Plex Media Server

Plex Media Server is a media streaming service that organizes your personal video, music, and photo collections and streams them to your devices.

## Overview

This deployment uses the official [plex-media-server Helm chart](https://github.com/plexinc/pms-docker/tree/master/charts/plex-media-server) from Plex Inc.

## Configuration

| Setting | Value |
|---------|-------|
| **Namespace** | `media` |
| **Node** | `rishik-worker1` (pinned via nodeSelector) |
| **Config Storage** | 20Gi Longhorn PVC (`plex-config`) |
| **Media Path** | `/media/rishik/Expansion` (external HDD, hostPath) |
| **GPU** | Intel QuickSync (`gpu.intel.com/i915: "1"`) for hardware transcoding |
| **LoadBalancer IP** | `192.168.1.200` (MetalLB) |
| **Ingress** | `plex.homelab` (Traefik with TLS) |
| **Port** | 32400 |

## Resource Limits

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Plex | 500m | 4000m (4 cores) | 1Gi | 4Gi |

## Hardware Acceleration

Plex is configured with Intel QuickSync hardware transcoding support:
- Device: `/dev/dri` mounted from host
- GPU resource: `gpu.intel.com/i915: "1"`
- Requires Intel GPU Device Plugin to be installed

## Loudness Analysis

**Settings > Library > Analyze audio tracks for loudness** is set to **Never**
(`LoudnessAnalysisBehavior=never`). Loudness analysis is disabled server-wide,
including scheduled processing and processing when media is added.

This is a Plex-owned preference, changed through its authenticated API and saved
in `Preferences.xml` on the `plex-config` PVC. It survives normal pod restarts
without changing the HelmRelease or restarting Plex. After restoring an older
config backup, reconfirm that this preference is still **Never**.

Already generated loudness metadata is retained. Playback, hardware transcoding,
and unrelated library analysis and maintenance settings are unchanged.

## Access

- **Local HTTPS**: `https://plex.homelab` (via Traefik ingress)
- **Remote/Direct**: `http://192.168.1.200:32400` (via LoadBalancer)

## Port Forwarding (for debugging)

To access Plex directly via port forwarding:
```bash
kubectl port-forward -n media svc/plex 32400:32400
```
Then access at: `http://localhost:32400`

## Files

- `helmrelease.yaml` - HelmRelease configuration
- `service.yaml` - LoadBalancer and ClusterIP services
- `ingress.yaml` - Traefik ingress for plex.homelab
- `certificate.yaml` - TLS certificate
- `pvc.yaml` - Longhorn PVC for config storage
- `configmap.yaml` - Environment variables (PLEX_UID, PLEX_GID)
- `networkpolicy.yaml` - Network policy for Plex pods
- `sealedsecret-plex-claim.yaml` - Sealed secret for Plex claim token

## Initial Setup

The deployment includes an init container that can restore a Plex database from a backup:
1. Copy `pms.tgz` to the PVC root
2. Init container will extract it to `/config/Library`
3. If library already exists, init container skips extraction

## Environment Variables

- `PLEX_UID` / `PLEX_GID`: Set via ConfigMap to match media file ownership
- `PLEX_CLAIM`: Optional claim token for linking to Plex account
- `ALLOWED_NETWORKS`: Empty; LAN, cluster, and remote clients must authenticate.
  The init container removes any persisted `allowedNetworks` preference before
  Plex starts, including after a database restore. Local and remote access paths
  remain available; clients that previously bypassed authentication must sign in.

## Health Checks

- **Liveness Probe**: HTTP GET `/identity` on port 32400
  - Initial delay: 120s
  - Period: 30s
  - Failure threshold: 7
  
- **Readiness Probe**: HTTP GET `/identity` on port 32400
  - Initial delay: 60s
  - Period: 15s
  - Failure threshold: 3
