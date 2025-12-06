# Bazarr

Bazarr is a companion application to Sonarr and Radarr that manages and downloads subtitles based on your requirements.

## Overview

This deployment uses the [bjw-s app-template Helm chart](https://github.com/bjw-s/helm-charts/tree/main/charts/other/app-template) with the linuxserver.io Bazarr container image.

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
| **Ingress** | `bazarr.homelab` |
| **Port** | 6767 |
| **TLS** | Enabled via cert-manager with `cluster-ca` ClusterIssuer |

## Resource Limits

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Bazarr | 100m | 1000m (1 core) | 256Mi | 1Gi |

## Access

- **HTTPS**: `https://bazarr.homelab` (via Traefik ingress with TLS)

## Files

- `helmrelease.yaml` - HelmRelease using bjw-s app-template chart
- `pvc.yaml` - 1Gi Longhorn PVC for config storage
- `configmap.yaml` - Environment variables (PUID, PGID, TZ)
- `ingress.yaml` - Traefik ingress for bazarr.homelab
- `certificate.yaml` - TLS certificate via cert-manager
- `kustomization.yaml` - Kustomize configuration

## Purpose

Bazarr automates subtitle management:
- Monitors Sonarr and Radarr libraries
- Searches for subtitles in multiple languages
- Downloads and organizes subtitle files
- Supports multiple subtitle providers
- Handles subtitle synchronization

## Setup

This is a new setup (not migrated from backup). Initial configuration required:
1. Connect to Sonarr and Radarr APIs
2. Configure subtitle languages
3. Add subtitle providers
4. Set download preferences

## Environment Variables

Configured via ConfigMap:
- `PUID=1000` - User ID for file permissions
- `PGID=1000` - Group ID for file permissions
- `TZ=America/Los_Angeles` - Timezone for scheduling

## Integration

Bazarr integrates with:
- **Sonarr**: For TV show subtitle management
- **Radarr**: For movie subtitle management
- Accesses media files via shared `/media` mount
