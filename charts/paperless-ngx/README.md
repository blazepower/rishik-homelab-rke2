# Paperless-ngx Helm Chart

A Helm chart for deploying Paperless-ngx document management system with PostgreSQL and Redis.

## Overview

This chart deploys:
- Paperless-ngx application (port 8000)
- PostgreSQL database (port 5432)
- Redis cache (port 6379)

## Prerequisites

- Kubernetes 1.19+
- Helm 3.0+
- Longhorn storage class available
- Sealed Secrets operator (for credentials)

## Installation

```bash
helm install paperless charts/paperless-ngx \
  --namespace paperless \
  --create-namespace
```

## Configuration

### Image Versions

| Component | Image | Tag |
|-----------|-------|-----|
| Paperless-ngx | `ghcr.io/paperless-ngx/paperless-ngx` | `2.13.5` |
| PostgreSQL | `docker.io/library/postgres` | `16-alpine` |
| Redis | `docker.io/library/redis` | `7-alpine` |

### Required Secrets

Create a SealedSecret named `paperless-credentials` with:

```yaml
apiVersion: bitnami.com/v1alpha1
kind: SealedSecret
metadata:
  name: paperless-credentials
  namespace: paperless
spec:
  encryptedData:
    postgresql-password: <sealed-value>
    paperless-secret-key: <sealed-value>
    paperless-admin-password: <sealed-value>
```

Generate secrets:
```bash
kubectl create secret generic paperless-credentials \
  --namespace=paperless \
  --from-literal=postgresql-password="$(openssl rand -base64 32)" \
  --from-literal=paperless-secret-key="$(openssl rand -base64 64 | tr -d '\n')" \
  --from-literal=paperless-admin-password="$(openssl rand -base64 32)" \
  --dry-run=client -o yaml | \
kubeseal --format yaml > sealedsecret-paperless-credentials.yaml
```

### Storage Configuration

Default persistent volume sizes:
- `data`: 5Gi (application data)
- `media`: 20Gi (document media files)
- `export`: 5Gi (exported documents)
- `consume`: 5Gi (incoming documents)
- `postgresql`: 8Gi (database)
- `redis`: 1Gi (cache)

All volumes use the `longhorn` storage class by default.

### Values

Key configuration values:

```yaml
# Application image
image:
  repository: ghcr.io/paperless-ngx/paperless-ngx
  tag: "2.13.5"

# Environment variables
env:
  PAPERLESS_URL: https://paperless.homelab
  PAPERLESS_ALLOWED_HOSTS: paperless.homelab
  PAPERLESS_OCR_LANGUAGE: eng

# Resources
resources:
  requests:
    cpu: 100m
    memory: 512Mi
  limits:
    cpu: "1"
    memory: 2Gi

# PostgreSQL
postgresql:
  enabled: true
  persistence:
    size: 8Gi

# Redis
redis:
  enabled: true
  persistence:
    size: 1Gi
```

## Security

All pods run with non-root security contexts:
```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  runAsGroup: 1000
  fsGroup: 1000
```

All containers have capability restrictions:
```yaml
securityContext:
  allowPrivilegeEscalation: false
  capabilities:
    drop:
      - ALL
```

## Health Checks

- **Paperless**: HTTP GET on `/api/` (port 8000)
- **PostgreSQL**: `pg_isready -U paperless_user -d paperless`
- **Redis**: `redis-cli ping`

## Upgrading

```bash
helm upgrade paperless charts/paperless-ngx \
  --namespace paperless
```

## Uninstallation

```bash
helm uninstall paperless --namespace paperless
```

**Note**: PVCs are not automatically deleted. Remove them manually if needed:
```bash
kubectl delete pvc -n paperless -l app.kubernetes.io/instance=paperless
```
