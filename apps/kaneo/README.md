# Kaneo Project Management

Kaneo is an open-source project management tool with a modern, user-friendly interface.

## Overview

This deployment uses the official [Kaneo Helm chart](https://github.com/usekaneo/kaneo) from the Kaneo project.

Kaneo consists of three main components:
- **API** - Backend service (ghcr.io/usekaneo/api) running on port 1337
- **Web** - Frontend service (ghcr.io/usekaneo/web) running on port 80
- **PostgreSQL** - Database (postgres:16-alpine) running on port 5432

## Configuration

| Setting | Value |
|---------|-------|
| **Namespace** | `kaneo` |
| **Database Storage** | 8Gi Longhorn PVC |
| **Ingress** | `kaneo.homelab` (Traefik with TLS) |
| **Path Routing** | `/api/*` → API service, `/*` → Web service |
| **API Port** | 1337 |
| **Web Port** | 80 |
| **Database** | PostgreSQL 16 (Alpine) |

## Resource Limits

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| PostgreSQL | 50m | 200m | 128Mi | 256Mi |
| API | 50m | 200m | 128Mi | 256Mi |
| Web | 25m | 100m | 64Mi | 128Mi |

## Access

- **HTTPS**: `https://kaneo.homelab` (via Traefik ingress with TLS)
- **API Endpoint**: `https://kaneo.homelab/api` (proxied to API service)

## Files

- `namespace.yaml` - Namespace definition
- `helmrelease.yaml` - HelmRelease configuration
- `ingress.yaml` - Traefik ingress with path-based routing
- `networkpolicy.yaml` - Network policy for Kaneo pods
- `kustomization.yaml` - Kustomize configuration
- `sealedsecret-kaneo-credentials.yaml` - Sealed secret for credentials

## Database Configuration

- **Database Name**: `kaneo`
- **Username**: `kaneo_user`
- **Password**: Stored in sealed secret `kaneo-credentials`
- **Persistence**: 8Gi Longhorn PVC with ReadWriteOnce access

## API Configuration

- **Image**: `ghcr.io/usekaneo/api:latest`
- **Port**: 1337
- **JWT Secret**: Stored in sealed secret `kaneo-credentials`
- **Registration**: Enabled by default (`disableRegistration: false`)

## Web Configuration

- **Image**: `ghcr.io/usekaneo/web:latest`
- **Port**: 80
- **API URL**: `https://kaneo.homelab/api`

## Security

The deployment uses SealedSecrets for:
- PostgreSQL password
- JWT access secret

## Network Policy

Network policy controls traffic to Kaneo pods, ensuring secure communication between components.
