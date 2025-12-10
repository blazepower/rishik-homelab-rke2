# Sure Finance

Sure is a community fork of the now-abandoned Maybe Finance personal finance app. It's a full-featured personal finance and wealth management application that helps you track your net worth, investments, and financial accounts.

## Overview

This deployment uses a local Helm chart located at `charts/maybe-finance` to deploy the Sure Finance application (appVersion 0.5.0).

Sure consists of:
- **Application** - Rails-based web application (ghcr.io/maybe-finance/maybe) running on port 3000
- **PostgreSQL** - Database running on port 5432
- **Redis** - Cache and job queue running on port 6379

## Links

- **Sure Repository**: https://github.com/we-promise/sure
- **Original Maybe Finance**: https://github.com/maybe-finance/maybe
- **Local Helm Chart**: `charts/maybe-finance` in this repository
- **Self-hosting Documentation**: https://github.com/we-promise/sure/blob/main/docs/hosting/docker.md

## Configuration

| Setting | Value |
|---------|-------|
| **Namespace** | `sure-finance` |
| **App Storage** | 2Gi Longhorn PVC |
| **Database Storage** | 4Gi Longhorn PVC |
| **Redis Storage** | 1Gi Longhorn PVC |
| **Ingress** | `sure.homelab` (Traefik with TLS) |
| **App Port** | 3000 |
| **Database** | PostgreSQL (via Bitnami chart) |
| **Cache** | Redis (via Bitnami chart) |

## Resource Limits

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Application | 50m | 200m | 256Mi | 512Mi |
| PostgreSQL | 50m | 200m | 128Mi | 256Mi |
| Redis | 25m | 100m | 64Mi | 128Mi |

## Access

- **HTTPS**: `https://sure.homelab` (via Traefik ingress with TLS)

## Port Forwarding (for debugging)

To access Sure Finance components directly via port forwarding:
```bash
# Application
kubectl port-forward -n sure-finance svc/sure-finance-maybe-finance 3000:3000

# PostgreSQL
kubectl port-forward -n sure-finance svc/sure-finance-postgresql 5432:5432

# Redis
kubectl port-forward -n sure-finance svc/sure-finance-redis-master 6379:6379
```
Then access at:
- App: `http://localhost:3000`
- PostgreSQL: `localhost:5432`
- Redis: `localhost:6379`

## Files

- `namespace.yaml` - Namespace definition
- `helmrelease.yaml` - HelmRelease configuration for local maybe-finance chart
- `ingress.yaml` - Traefik ingress configuration
- `certificate.yaml` - cert-manager Certificate for TLS
- `networkpolicy.yaml` - Network policy for Sure Finance pods
- `kustomization.yaml` - Kustomize configuration
- `sealedsecret-sure-credentials.yaml` - Sealed secret for credentials (placeholder)

## Creating Sealed Secrets

The application requires several secrets to be created and sealed before deployment. Follow these steps:

### 1. Generate Required Values

Generate secure values for all secrets:
```bash
POSTGRES_ADMIN_PASSWORD=$(openssl rand -base64 32)
POSTGRES_USER_PASSWORD=$(openssl rand -base64 32)
REDIS_PASSWORD=$(openssl rand -base64 32)
SECRET_KEY_BASE=$(openssl rand -hex 64)
```

### 2. Create a Temporary Secret

Create a temporary secret file using the generated values:
```bash
cat <<EOF > /tmp/sure-credentials.yaml
apiVersion: v1
kind: Secret
metadata:
  name: sure-credentials
  namespace: sure-finance
type: Opaque
stringData:
  postgres-password: "\${POSTGRES_ADMIN_PASSWORD}"
  password: "\${POSTGRES_USER_PASSWORD}"
  redis-password: "\${REDIS_PASSWORD}"
  SECRET_KEY_BASE: "\${SECRET_KEY_BASE}"
EOF
```

### 3. Seal the Secret

Use kubeseal to encrypt the secret:
```bash
kubeseal --format=yaml --cert=<path-to-sealed-secrets-cert.pem> \
  < /tmp/sure-credentials.yaml > apps/sure-finance/sealedsecret-sure-credentials.yaml
```

### 4. Clean Up

Remove the temporary file:
```bash
rm /tmp/sure-credentials.yaml
```

### 5. Apply the Configuration

The sealed secret will be automatically applied when the Flux GitOps system syncs the repository.

## Database Configuration

- **Database Name**: `maybe`
- **Username**: `maybe`
- **Passwords**: Stored in sealed secret `sure-credentials`
- **Persistence**: 4Gi Longhorn PVC with ReadWriteOnce access

## Redis Configuration

- **Architecture**: Standalone
- **Password**: Stored in sealed secret `sure-credentials`
- **Persistence**: 1Gi Longhorn PVC with ReadWriteOnce access

## Application Configuration

- **Image**: `ghcr.io/maybe-finance/maybe:0.5.0`
- **Port**: 3000
- **Environment Variables**: Configured via sealed secret `sure-credentials`
  - `SECRET_KEY_BASE`: Rails secret key base (required)
  - `SELF_HOSTING_ENABLED`: Set to "true" for self-hosted deployment

## Post-Deployment Setup

After the application is deployed and running:

1. Access the application at `https://sure.homelab`

2. The application will run database migrations automatically on first start

3. You can seed the database with sample data (optional):
   ```bash
   kubectl exec -it -n sure-finance deployment/sure-finance-maybe-finance -- rails db:seed
   ```

4. Default login credentials after seeding (if you ran db:seed):
   - **Email**: `user@example.com`
   - **Password**: `Password1!`

5. **IMPORTANT**: Change the default password immediately after first login!

6. Create your first account and start managing your finances

## Security

The deployment uses:
- **SealedSecrets** for PostgreSQL, Redis passwords, and Rails secret key
- **TLS** via cert-manager with cluster-ca ClusterIssuer
- **Network Policies** to control traffic between components
- **Longhorn** for encrypted persistent storage

## Network Policy

Network policy controls traffic to Sure Finance pods:
- Allows Traefik ingress controller access to the application (port 3000)
- Allows internal namespace communication for PostgreSQL (5432) and Redis (6379)
- Denies all other ingress traffic by default

## Troubleshooting

### Check Application Logs
```bash
kubectl logs -n sure-finance deployment/sure-finance-maybe-finance -f
```

### Check PostgreSQL Logs
```bash
kubectl logs -n sure-finance statefulset/sure-finance-postgresql -f
```

### Check Redis Logs
```bash
kubectl logs -n sure-finance statefulset/sure-finance-redis-master -f
```

### Verify Secrets
```bash
kubectl get secret -n sure-finance sure-credentials -o yaml
```

### Check HelmRelease Status
```bash
kubectl get helmrelease -n sure-finance sure-finance -o yaml
```

### Verify Ingress
```bash
kubectl get ingress -n sure-finance
kubectl describe ingress -n sure-finance sure-finance
```

## Backup and Restore

### Backup PostgreSQL Database
```bash
kubectl exec -n sure-finance statefulset/sure-finance-postgresql -- \
  pg_dump -U maybe maybe > sure-finance-backup-$(date +%Y%m%d).sql
```

### Restore PostgreSQL Database
```bash
kubectl exec -i -n sure-finance statefulset/sure-finance-postgresql -- \
  psql -U maybe maybe < sure-finance-backup-YYYYMMDD.sql
```

## Upgrading

The application is automatically upgraded when the Helm chart or app version is updated in `helmrelease.yaml`. Flux will detect the changes and apply them.

To manually trigger an upgrade:
```bash
flux reconcile helmrelease -n sure-finance sure-finance
```
