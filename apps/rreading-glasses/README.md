# rreading-glasses

rreading-glasses is a metadata backend service that enhances book information by integrating with Hardcover.app API.

## Overview

- **Image**: `docker.io/blampe/rreading-glasses:hardcover`
- **Port**: 8080
- **Access**: Internal only (ClusterIP service, no ingress)
- **Namespace**: media
- **Node**: rishik-worker1

## Architecture

rreading-glasses consists of two components:
1. **Application Container**: The metadata service
2. **PostgreSQL Database**: Data persistence for cached metadata

## Storage

- **PostgreSQL PVC**: 8Gi Longhorn volume for database data

## Configuration

### Environment Variables (via SealedSecret)

The application requires three secrets:

1. **HARDCOVER_API_KEY**: API key for Hardcover.app
   - Obtain from: https://hardcover.app/settings/api
   
2. **POSTGRES_PASSWORD**: Password for PostgreSQL database access
   - Auto-generated during secret creation
   
3. **RG_DATABASE_URL**: Full PostgreSQL connection string
   - Format: `postgresql://rreading_user:PASSWORD@rreading-glasses-postgresql:5432/rreading_glasses`

### PostgreSQL Configuration

- Database: `rreading_glasses`
- User: `rreading_user`
- Port: 5432
- Service: `rreading-glasses-postgresql`

## Sealing Secrets

To create and seal the credentials:

```bash
# Generate a secure PostgreSQL password
POSTGRES_PASSWORD="$(openssl rand -base64 32)"

# Create the sealed secret
kubectl create secret generic rreading-glasses-credentials \
  --namespace=media \
  --from-literal=HARDCOVER_API_KEY="your-hardcover-api-key-here" \
  --from-literal=POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
  --from-literal=RG_DATABASE_URL="postgresql://rreading_user:${POSTGRES_PASSWORD}@rreading-glasses-postgresql:5432/rreading_glasses" \
  --dry-run=client -o yaml | \
kubeseal --format yaml \
  --controller-name=sealed-secrets \
  --controller-namespace=flux-system \
  > apps/rreading-glasses/sealedsecret-rreading-glasses-credentials.yaml
```

## Security

### Pod Security
- Runs as non-root user (UID 1000, GID 1000)
- FSGroup set to 1000 for volume permissions
- All capabilities dropped
- Read-only root filesystem where possible

### Network Policy
- **Ingress**: Only accepts connections from Bookshelf on port 8080
- **Egress**: 
  - DNS (port 53)
  - PostgreSQL database (port 5432) within media namespace
  - HTTPS (port 443) for Hardcover.app API access

## Resource Limits

### Application
- **Requests**: 50m CPU, 128Mi memory
- **Limits**: 200m CPU, 256Mi memory

### PostgreSQL
- **Requests**: 50m CPU, 128Mi memory
- **Limits**: 200m CPU, 256Mi memory

## Integration

### Used By
- **Bookshelf**: Connects to `http://rreading-glasses:8080` for enhanced metadata

## Troubleshooting

### Check pod status
```bash
kubectl get pods -n media -l app.kubernetes.io/name=rreading-glasses
kubectl get pods -n media -l app.kubernetes.io/name=postgresql
```

### View application logs
```bash
kubectl logs -n media -l app.kubernetes.io/name=rreading-glasses
```

### View PostgreSQL logs
```bash
kubectl logs -n media -l app.kubernetes.io/instance=rreading-glasses-postgresql
```

### Test database connection
```bash
kubectl exec -n media -it <rreading-glasses-pod> -- wget -O- http://rreading-glasses-postgresql:5432
```

### Check database health
```bash
kubectl exec -n media -it <postgresql-pod> -- pg_isready -U rreading_user -d rreading_glasses
```

### Verify secret is properly sealed
```bash
kubectl get secret rreading-glasses-credentials -n media -o yaml
```

## API Endpoints

The service exposes these endpoints:
- `/health` - Health check endpoint
- `/api/books/*` - Book metadata API

## Notes

- This service is internal-only and should not be exposed via ingress
- The PostgreSQL database is dedicated to this service
- Metadata is cached in PostgreSQL to reduce API calls to Hardcover.app
- Ensure you have a valid Hardcover.app API key before deployment
- The service will fail to start without proper database credentials
