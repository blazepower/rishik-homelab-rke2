# Overseerr

Overseerr is a request management and media discovery tool built to work with your existing Plex ecosystem. It provides a user-friendly interface for requesting new content.

## Overview

This deployment uses the [bjw-s app-template Helm chart](https://github.com/bjw-s/helm-charts/tree/main/charts/other/app-template) with the linuxserver.io Overseerr container image.

## Configuration

| Setting | Value |
|---------|-------|
| **Namespace** | `media` |
| **Node** | `rishik-worker1` (pinned via nodeSelector) |
| **Storage Class** | `longhorn` |
| **Config PVC Size** | 1Gi |
| **User/Group** | PUID=1000, PGID=1000 |
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
