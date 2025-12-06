# Sonarr

Sonarr is a PVR for Usenet and BitTorrent users. It can monitor multiple RSS feeds for new episodes of your favorite shows and will grab, sort, and rename them.

## Overview

This deployment uses the [bjw-s app-template Helm chart](https://github.com/bjw-s/helm-charts/tree/main/charts/other/app-template) with the linuxserver.io Sonarr container image.

## Configuration

| Setting | Value |
|---------|-------|
| **Namespace** | `media` |
| **Node** | `rishik-worker1` (pinned via nodeSelector) |
| **Storage Class** | `longhorn` |
| **Config PVC Size** | 1Gi |
| **User/Group** | PUID=1000, PGID=1000 |
| **Timezone** | America/Los_Angeles |
| **Media Mount** | `/media/rishik/Expansion` → `/media` (hostPath) |
| **Service Type** | ClusterIP (internal only) |
| **Ingress** | `sonarr.homelab` |
| **Port** | 8989 |
| **TLS** | Enabled via cert-manager with `cluster-ca` ClusterIssuer |

## Resource Limits

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Sonarr | 100m | 1000m (1 core) | 256Mi | 1Gi |

## Access

- **HTTPS**: `https://sonarr.homelab` (via Traefik ingress with TLS)

## Files

- `helmrelease.yaml` - HelmRelease using bjw-s app-template chart
- `pvc.yaml` - 1Gi Longhorn PVC for config storage
- `configmap.yaml` - Environment variables (PUID, PGID, TZ)
- `ingress.yaml` - Traefik ingress for sonarr.homelab
- `certificate.yaml` - TLS certificate via cert-manager
- `kustomization.yaml` - Kustomize configuration

## Purpose

Sonarr automates the management of TV shows:
- Monitors for new episodes
- Searches indexers (via Prowlarr) for releases
- Sends download requests to SABnzbd
- Organizes and renames files after download
- Updates library metadata

## Directory Structure

- `/media/downloads/complete/tv/` - Completed TV downloads
- `/media/downloads/incomplete/` - In-progress downloads

## Migration

Configuration should be restored from backup to maintain:
- TV show library
- Quality profiles
- Download client settings
- Indexer connections (via Prowlarr)

## Environment Variables

Configured via ConfigMap:
- `PUID=1000` - User ID for file permissions
- `PGID=1000` - Group ID for file permissions
- `TZ=America/Los_Angeles` - Timezone for scheduling

## Integration

Sonarr integrates with:
- **Prowlarr**: For indexer management
- **SABnzbd**: For downloading content
- **Plex**: For media library updates
