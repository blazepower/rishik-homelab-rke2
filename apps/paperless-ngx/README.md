# Paperless-ngx Document Management System

Paperless-ngx is a community-supported open-source document management system that transforms your physical documents into a searchable online archive so you can keep, well, less paper.

## Overview

This deployment uses a local Helm chart located at `charts/paperless-ngx` in this repository.

Paperless-ngx consists of:
- **Paperless-ngx Application** - Main web interface and document processing (ghcr.io/paperless-ngx/paperless-ngx) running on port 8000
- **PostgreSQL** - Database (PostgreSQL 18 Alpine) running on port 5432
- **Redis** - Cache and task queue running on port 6379

## Configuration

| Setting | Value |
|---------|-------|
| **Namespace** | `paperless` |
| **Application Port** | 8000 |
| **Database** | PostgreSQL 18 (Alpine) |
| **Database Storage** | 8Gi Longhorn PVC |
| **Document Storage** | Multiple Longhorn PVCs (data: 5Gi, media: 20Gi, export: 5Gi, consume: 5Gi) |
| **Ingress** | `paperless.homelab` (Traefik with TLS) |
| **OCR Language** | English (eng) - can be customized |

## Resource Limits

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Paperless-ngx | 100m | 1 | 512Mi | 2Gi |
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

Before deploying, create a SealedSecret with these keys:

- **postgresql-password**: Shared application and PostgreSQL password
- **paperless-admin-password**: Initial administrator password
- **paperless-secret-key**: Persistent Django signing key

The chart maps these keys to the corresponding environment variables. Preserve
the original signing key across upgrades; v3 requires it explicitly.

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
- **Password**: The password stored under `paperless-admin-password`

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
kubectl port-forward -n paperless svc/paperless-paperless-ngx 8000:8000

# PostgreSQL
kubectl port-forward -n paperless svc/paperless-paperless-ngx-postgresql 5432:5432
```

Then access at:
- Web: `http://localhost:8000`
- PostgreSQL: `localhost:5432`

## Files

- `namespace.yaml` - Namespace definition
- `helmrelease.yaml` - HelmRelease configuration for local paperless-ngx chart
- `ingress.yaml` - Traefik ingress configuration
- `certificate.yaml` - cert-manager Certificate for TLS
- `networkpolicy.yaml` - Network policy for secure pod communication
- `kustomization.yaml` - Kustomize configuration
- `sealedsecret-paperless-credentials.yaml` - Sealed secret for credentials

## Database Configuration

- **Database Name**: `paperless`
- **Username**: `paperless_user`
- **Password**: Stored in sealed secret `paperless-credentials`
- **Host**: `paperless-paperless-ngx-postgresql` (internal service name)
- **Port**: 5432
- **Persistence**: 8Gi Longhorn PVC with ReadWriteOnce access

## Security

The deployment uses SealedSecrets for:
- PostgreSQL password (`postgresql-password`)
- Paperless admin password (`paperless-admin-password`)
- Django secret key (`paperless-secret-key`)

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

Update the immutable image in `charts/paperless-ngx/values.yaml`, align
`appVersion`, and increment the local chart's `version` in `Chart.yaml`.
Flux uses `ChartVersion` reconciliation, so template/default changes also need
a chart-version increment.

For the v3 migration, require the completed v2.20.15 source schema, a coordinated
native PostgreSQL plus data/media/consume/export backup, an isolated old-version
restore, and a target-version migration rehearsal. Preserve the existing signing
key, database credentials and explicit `PAPERLESS_DBENGINE=postgresql`.

The chart explicitly preserves the v2 OCR/archive defaults using
`PAPERLESS_OCR_MODE=auto` and `PAPERLESS_ARCHIVE_FILE_GENERATION=always`.
`PAPERLESS_CONSUMER_DELETE_DUPLICATES=true` prevents duplicate documents from
being imported and removes duplicate inputs from the consume directory.
The existing `Recreate` strategy keeps the application single-writer. A
ten-minute startup probe protects migrations and the first Tantivy index rebuild
from premature liveness restarts.

V3 intentionally clears old processing-task history, not documents. This reset
requires operator approval; retain the complete pre-upgrade history in the
recovery backup. Saved full-text queries involving notes/custom fields also
need review under the upstream v3 search syntax.

Suspend the HelmRelease and block KEDA/ingress activation during the coordinated
maintenance window. Apply chart and release changes through Flux before
resuming. The HelmRelease deliberately omits `spec.suspend` so a GitOps
reconciliation does not override an operator's temporary suspension.
After successful migration, restore the original KEDA annotations, ingress
backend and scale-to-zero behavior.

Automatic upgrade remediation is disabled: image rollback cannot undo schema or
file migrations. Recover with the matching database, files and application
version instead of starting an old image against a migrated database.

## Additional Resources

- [Paperless-ngx Documentation](https://docs.paperless-ngx.com/)
- [Local Helm Chart](../../charts/paperless-ngx)
- [Paperless-ngx GitHub](https://github.com/paperless-ngx/paperless-ngx)
