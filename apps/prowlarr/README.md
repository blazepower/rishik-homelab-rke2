# Prowlarr

Prowlarr is an indexer manager/proxy built on the popular *arr .net/reactjs base stack. It integrates with various PVR applications including Sonarr and Radarr.

## Overview

This deployment uses the [bjw-s app-template Helm chart](https://github.com/bjw-s/helm-charts/tree/main/charts/other/app-template) with the linuxserver.io Prowlarr container image.

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
| **Ingress** | `prowlarr.homelab` |
| **Port** | 9696 |
| **TLS** | Enabled via cert-manager with `cluster-ca` ClusterIssuer |

## Resource Limits

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Prowlarr | 100m | 1000m (1 core) | 256Mi | 1Gi |

## Access

- **HTTPS**: `https://prowlarr.homelab` (via Traefik ingress with TLS)

## Port Forwarding (for debugging)

To access Prowlarr directly via port forwarding:
```bash
kubectl port-forward -n media svc/prowlarr 9696:9696
```
Then access at: `http://localhost:9696`

## Files

- `helmrelease.yaml` - HelmRelease using bjw-s app-template chart
- `pvc.yaml` - 1Gi Longhorn PVC for config storage
- `configmap.yaml` - Environment variables (PUID, PGID, TZ)
- `ingress.yaml` - Traefik ingress for prowlarr.homelab
- `certificate.yaml` - TLS certificate via cert-manager
- `kustomization.yaml` - Kustomize configuration

## Purpose

Prowlarr serves as the central hub for managing indexers used by Sonarr, Radarr, and other *arr applications. It:
- Centralizes indexer configuration
- Automatically syncs indexers to connected applications
- Provides unified search across all indexers
- Simplifies indexer management

## Migration

Configuration should be restored from backup to maintain indexer settings and application integrations.

## Environment Variables

Configured via ConfigMap:
- `PUID=1000` - User ID for file permissions
- `PGID=1000` - Group ID for file permissions
- `TZ=America/Los_Angeles` - Timezone for scheduling
