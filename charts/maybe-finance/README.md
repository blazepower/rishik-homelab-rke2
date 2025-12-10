# Maybe Finance Helm Chart

A Helm chart for deploying Maybe Finance personal finance management application with PostgreSQL.

## Overview

This chart deploys:
- Maybe Finance application (port 3000)
- PostgreSQL database (port 5432)

## Prerequisites

- Kubernetes 1.19+
- Helm 3.0+
- Longhorn storage class available
- Sealed Secrets operator (for credentials)

## Installation

```bash
helm install sure-finance charts/maybe-finance \
  --namespace sure-finance \
  --create-namespace
```

## Configuration

### Image Versions

| Component | Image | Tag |
|-----------|-------|-----|
| Maybe Finance | `ghcr.io/maybe-finance/maybe` | `0.1.0-alpha.17` |
| PostgreSQL | `docker.io/library/postgres` | `16-alpine` |

### Required Secrets

Create a SealedSecret named `sure-credentials` with:

```yaml
apiVersion: bitnami.com/v1alpha1
kind: SealedSecret
metadata:
  name: sure-credentials
  namespace: sure-finance
spec:
  encryptedData:
    postgres-password: <sealed-value>
    secret-key-base: <sealed-value>
```

Generate secrets:
```bash
kubectl create secret generic sure-credentials \
  --namespace=sure-finance \
  --from-literal=postgres-password="$(openssl rand -base64 32)" \
  --from-literal=secret-key-base="$(openssl rand -hex 64)" \
  --dry-run=client -o yaml | \
kubeseal --format yaml > sealedsecret-sure-credentials.yaml
```

### Storage Configuration

Default persistent volume sizes:
- `app-storage`: 2Gi (application storage)
- `postgresql`: 4Gi (database)

All volumes use the `longhorn` storage class by default.

### Values

Key configuration values:

```yaml
# Application image
image:
  repository: ghcr.io/maybe-finance/maybe
  tag: "0.1.0-alpha.17"

# Environment variables
env:
  SELF_HOSTING_ENABLED: "true"
  RAILS_FORCE_SSL: "false"
  RAILS_ASSUME_SSL: "false"
  GOOD_JOB_EXECUTION_MODE: "async"
  POSTGRES_DB: maybe_production
  POSTGRES_USER: maybe_user

# Resources
resources:
  requests:
    cpu: 50m
    memory: 256Mi
  limits:
    cpu: 200m
    memory: 512Mi

# PostgreSQL
postgresql:
  enabled: true
  persistence:
    size: 4Gi
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

- **Maybe Finance**: HTTP GET on `/` (port 3000)
- **PostgreSQL**: `pg_isready -U maybe_user -d maybe_production`

## Upgrading

```bash
helm upgrade sure-finance charts/maybe-finance \
  --namespace sure-finance
```

## Uninstallation

```bash
helm uninstall sure-finance --namespace sure-finance
```

**Note**: PVCs are not automatically deleted. Remove them manually if needed:
```bash
kubectl delete pvc -n sure-finance -l app.kubernetes.io/instance=sure-finance
```
