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

## Access

- **Local HTTPS**: `https://plex.homelab` (via Traefik ingress)
- **Remote/Direct**: `http://192.168.1.200:32400` (via LoadBalancer)

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
- `ALLOWED_NETWORKS`: Network ranges allowed to access Plex (192.168.1.0/24, 10.42.0.0/16, 10.43.0.0/16)

## Health Checks

- **Liveness Probe**: HTTP GET `/identity` on port 32400
  - Initial delay: 120s
  - Period: 30s
  - Failure threshold: 7
  
- **Readiness Probe**: HTTP GET `/identity` on port 32400
  - Initial delay: 60s
  - Period: 15s
  - Failure threshold: 3
