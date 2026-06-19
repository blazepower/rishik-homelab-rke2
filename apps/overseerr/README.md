# Overseerr → Seerr

This deployment now runs **[Seerr](https://github.com/seerr-team/seerr)** (`ghcr.io/seerr-team/seerr`), the actively maintained successor to Overseerr/Jellyseerr. On first boot, Seerr automatically migrates the existing Overseerr SQLite DB (`/app/config/db/db.sqlite3`) and `settings.json` — no manual steps required.

The Helm release name, deployment name, PVC (`overseerr-config`), Service, and Ingress hostname are intentionally **kept as `overseerr`** to avoid breaking Flux state, ingress URLs, and dependent apps (Sonarr/Radarr webhooks, Homepage widgets). Rename later if desired.

## Overview

This deployment uses the [bjw-s app-template Helm chart](https://github.com/bjw-s/helm-charts/tree/main/charts/other/app-template) with the official Seerr container image.

## Configuration

| Setting | Value |
|---------|-------|
| **Namespace** | `media` |
| **Node** | `rishik-worker1` (pinned via nodeSelector) |
| **Storage Class** | `longhorn` |
| **Config PVC Size** | 1Gi |
| **User/Group** | runAsUser=1000 / runAsGroup=1000 (image's `node` user; PUID/PGID env removed — no longer applicable) |
| **Timezone** | America/Los_Angeles |
| **Service Type** | ClusterIP (internal only) |
| **Ingress** | `overseerr.homelab` |
| **Port** | 5055 |
| **TLS** | Enabled via cert-manager with `cluster-ca` ClusterIssuer |

## Resource Limits

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Overseerr | 100m | 1000m (1 core) | 256Mi | 1Gi |

## Access

- **HTTPS**: `https://overseerr.homelab` (via Traefik ingress with TLS)

## Port Forwarding (for debugging)

To access Overseerr directly via port forwarding:
```bash
kubectl port-forward -n media svc/overseerr 5055:5055
```
Then access at: `http://localhost:5055`

## Files

- `helmrelease.yaml` - HelmRelease using bjw-s app-template chart
- `pvc.yaml` - 1Gi Longhorn PVC for config storage
- `configmap.yaml` - Environment variables (PUID, PGID, TZ)
- `ingress.yaml` - Traefik ingress for overseerr.homelab
- `certificate.yaml` - TLS certificate via cert-manager
- `kustomization.yaml` - Kustomize configuration

## Purpose

Overseerr provides a centralized interface for media requests:
- User-friendly media discovery
- Request approval workflows
- Integrates with Plex for existing content detection
- Automatically sends requests to Sonarr/Radarr
- Notifications for request status updates
- User management and permissions

## Setup

This is a new setup (not migrated from backup). Initial configuration required:
1. Connect to Plex server
2. Configure Sonarr and Radarr connections
3. Set up user authentication (Plex OAuth)
4. Configure request approval settings
5. Set up notification channels (optional)

## Environment Variables

Configured via ConfigMap:
- `PUID=1000` - User ID for file permissions
- `PGID=1000` - Group ID for file permissions
- `TZ=America/Los_Angeles` - Timezone for scheduling

## Integration

Overseerr integrates with:
- **Plex**: For authentication and existing content detection
- **Sonarr**: For TV show requests
- **Radarr**: For movie requests
- Provides a bridge between end users and the automation stack

## Features

- **Media Discovery**: Browse popular, trending, and upcoming content
- **Request Management**: Submit and track content requests
- **User Management**: Multi-user support with role-based permissions
- **Notifications**: Email, Discord, Slack, and more
- **Quality Profiles**: Select quality and resolution preferences per request
