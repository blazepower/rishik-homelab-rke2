# Paperless-ngx Document Management System

Paperless-ngx is a community-supported open-source document management system that transforms your physical documents into a searchable online archive so you can keep, well, less paper.

## Overview

This deployment uses the [zekker6/paperless Helm chart](https://artifacthub.io/packages/helm/zekker6/paperless) (version 10.8.0) from the zekker6 Helm repository.

Paperless-ngx consists of:
- **Paperless-ngx Application** - Main web interface and document processing (ghcr.io/paperless-ngx/paperless-ngx) running on port 8000
- **PostgreSQL** - Database (postgres:16-alpine) running on port 5432
- **Redis** - Disabled (paperless-ngx can run without it for smaller deployments)

## Configuration

| Setting | Value |
|---------|-------|
| **Namespace** | `paperless` |
| **Application Port** | 8000 |
| **Database** | PostgreSQL 16 (Alpine) |
| **Database Storage** | 8Gi Longhorn PVC |
| **Document Storage** | Multiple Longhorn PVCs (data: 5Gi, media: 20Gi, export: 5Gi, consume: 5Gi) |
| **Ingress** | `paperless.homelab` (Traefik with TLS) |
| **OCR Language** | English (eng) - can be customized |

## Resource Limits

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Paperless-ngx | 50m | 200m | 128Mi | 256Mi |
| PostgreSQL | 50m | 200m | 128Mi | 256Mi |

## Storage Volumes

Paperless-ngx uses multiple persistent volumes for different purposes:

- **data** (5Gi) - Application data and configuration at `/usr/src/paperless/data`
- **media** (20Gi) - Processed and stored documents at `/usr/src/paperless/media`
- **export** (5Gi) - Document exports at `/usr/src/paperless/export`
- **consume** (5Gi) - Incoming documents to be processed at `/usr/src/paperless/consume`

All volumes use the Longhorn storage class with ReadWriteOnce access mode.

## Access

- **HTTPS**: `https://paperless.homelab` (via Traefik ingress with TLS)
- **Default Login**: Use the credentials you set in the sealed secret

## Initial Setup

### 1. Create and Seal Secrets

Before deploying, you need to create a SealedSecret with the required credentials. The sealed secret must contain three keys:

- **PAPERLESS_DBPASS**: Password for the PostgreSQL database
- **PAPERLESS_ADMIN_PASSWORD**: Admin credentials for the Paperless-ngx web interface
- **PAPERLESS_SECRET_KEY**: Django secret key (64+ characters recommended)

**Instructions:**

1. See the comments in `sealedsecret-paperless-credentials.yaml` for the exact command to generate and seal secrets
2. Refer to [Sealed Secrets Documentation](../../docs/sealed-secrets.md) for detailed information on working with sealed secrets

### 2. Commit and Deploy

```bash
git add apps/paperless-ngx/sealedsecret-paperless-credentials.yaml
git commit -m "Add paperless-ngx sealed secrets"
git push
```

Flux will automatically reconcile and deploy paperless-ngx to your cluster.

### 3. Wait for Deployment

Monitor the deployment:

```bash
# Watch the Flux reconciliation
flux get helmreleases -n paperless

# Check pod status
kubectl get pods -n paperless

# View logs
kubectl logs -n paperless -l app.kubernetes.io/name=paperless -f
```

### 4. First Login

Once deployed, navigate to `https://paperless.homelab` and log in with:
- **Username**: `admin` (default; to customize, set `PAPERLESS_ADMIN_USER` in your HelmRelease)
- **Password**: The password you set in `PAPERLESS_ADMIN_PASSWORD`

## Post-Deployment Configuration

After the initial deployment, you should:

1. **Configure OCR Languages**: If you need additional languages, update the `PAPERLESS_OCR_LANGUAGE` environment variable in `helmrelease.yaml`
2. **Set up Document Consumption**: 
   - Upload documents via the web interface
   - Use the consume directory (accessible via PVC)
   - Configure email consumption if needed
3. **Configure Document Processing**: Set up tags, correspondents, and document types
4. **Set up Backups**: Ensure your Longhorn volumes are backed up regularly

## Port Forwarding (for debugging)

To access Paperless-ngx components directly via port forwarding:

```bash
# Web interface
kubectl port-forward -n paperless svc/paperless 8000:8000

# PostgreSQL
kubectl port-forward -n paperless svc/paperless-postgresql 5432:5432
```

Then access at:
- Web: `http://localhost:8000`
- PostgreSQL: `localhost:5432`

## Files

- `namespace.yaml` - Namespace definition
- `helmrelease.yaml` - HelmRelease configuration for zekker6/paperless chart
- `ingress.yaml` - Traefik ingress configuration
- `certificate.yaml` - cert-manager Certificate for TLS
- `networkpolicy.yaml` - Network policy for secure pod communication
- `kustomization.yaml` - Kustomize configuration
- `sealedsecret-paperless-credentials.yaml` - Sealed secret for credentials

## Database Configuration

- **Database Name**: `paperless`
- **Username**: `paperless_user`
- **Password**: Stored in sealed secret `paperless-credentials`
- **Host**: `paperless-postgresql` (internal service name)
- **Port**: 5432
- **Persistence**: 8Gi Longhorn PVC with ReadWriteOnce access

## Security

The deployment uses SealedSecrets for:
- PostgreSQL password (`PAPERLESS_DBPASS`)
- Paperless admin password (`PAPERLESS_ADMIN_PASSWORD`)
- Django secret key (`PAPERLESS_SECRET_KEY`)

Network policies restrict traffic to:
- Traefik ingress controller access on port 8000
- Internal namespace communication for PostgreSQL on port 5432

## Troubleshooting

### Pod Not Starting

Check logs:
```bash
kubectl logs -n paperless -l app.kubernetes.io/name=paperless
kubectl logs -n paperless -l app.kubernetes.io/name=postgresql
```

### Database Connection Issues

Verify the database is running:
```bash
kubectl get pods -n paperless
kubectl logs -n paperless paperless-postgresql-0
```

### Storage Issues

Check PVC status:
```bash
kubectl get pvc -n paperless
```

### Ingress Not Working

Verify ingress and certificate:
```bash
kubectl get ingress -n paperless
kubectl get certificate -n paperless
```

## Upgrading

To upgrade paperless-ngx, update the chart version in `helmrelease.yaml`:

```yaml
chart:
  spec:
    chart: paperless
    version: x.y.z  # Update this
```

Commit and push the change. Flux will automatically handle the upgrade.

## Additional Resources

- [Paperless-ngx Documentation](https://docs.paperless-ngx.com/)
- [zekker6/paperless Helm Chart](https://artifacthub.io/packages/helm/zekker6/paperless)
- [Paperless-ngx GitHub](https://github.com/paperless-ngx/paperless-ngx)
