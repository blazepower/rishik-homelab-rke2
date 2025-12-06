# SABnzbd

SABnzbd is a free and open-source binary newsreader. It simplifies the process of downloading from Usenet by automating the downloading, verification, repairing, and extraction of files.

## Overview

This deployment uses the [bjw-s app-template Helm chart](https://github.com/bjw-s/helm-charts/tree/main/charts/other/app-template) with the linuxserver.io SABnzbd container image.

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
| **Ingress** | `sabnzbd.homelab` |
| **Port** | 8080 |
| **TLS** | Enabled via cert-manager with `cluster-ca` ClusterIssuer |

## Resource Limits

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| SABnzbd | 200m | 2000m (2 cores) | 512Mi | 2Gi |

**Note**: SABnzbd has higher resource limits than other *arr apps to handle CPU-intensive unpacking and verification tasks.

## Access

- **HTTPS**: `https://sabnzbd.homelab` (via Traefik ingress with TLS)

## Files

- `helmrelease.yaml` - HelmRelease using bjw-s app-template chart
- `pvc.yaml` - 1Gi Longhorn PVC for config storage
- `configmap.yaml` - Environment variables (PUID, PGID, TZ)
- `ingress.yaml` - Traefik ingress for sabnzbd.homelab
- `certificate.yaml` - TLS certificate via cert-manager
- `kustomization.yaml` - Kustomize configuration

## Purpose

SABnzbd serves as the download client for the *arr stack:
- Downloads NZB files from Usenet
- Automatically verifies and repairs downloads using PAR2 files
- Extracts archives (RAR, ZIP, etc.)
- Manages download queue and priorities
- Reports completion status to Sonarr/Radarr

## Directory Structure

- `/media/downloads/complete/tv/` - Completed TV downloads
- `/media/downloads/complete/movies/` - Completed movie downloads
- `/media/downloads/incomplete/` - In-progress downloads

## Migration

Configuration should be restored from backup to maintain:
- Usenet server settings
- Category configurations
- Download paths
- API keys (used by Sonarr/Radarr)

## Environment Variables

Configured via ConfigMap:
- `PUID=1000` - User ID for file permissions
- `PGID=1000` - Group ID for file permissions
- `TZ=America/Los_Angeles` - Timezone for scheduling

## Integration

SABnzbd integrates with:
- **Sonarr**: Receives TV show download requests
- **Radarr**: Receives movie download requests
- Sends completion notifications back to requesting applications
