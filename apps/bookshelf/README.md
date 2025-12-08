# Bookshelf

Bookshelf is a Readarr fork optimized for book management in the homelab environment.

## Overview

- **Image**: `ghcr.io/pennydreadful/bookshelf:hardcover-v0.4.10.2765`
- **Port**: 8787
- **Access URL**: https://bookshelf.homelab
- **Namespace**: media
- **Node**: rishik-worker1

## Architecture

Bookshelf serves as the primary book management interface, integrating with:
- **rreading-glasses**: Metadata backend for enhanced book information
- **Shared Media**: Accesses `/media/rishik/Expansion/books` for book storage

## Storage

- **Config PVC**: 1Gi Longhorn volume for application configuration
- **Media Mount**: hostPath mount of `/media/rishik/Expansion` to `/media`
  - Books are stored at: `/media/books`

## Configuration

Environment variables are managed via ConfigMap:
- `PUID`: 1000
- `PGID`: 1000
- `TZ`: America/Los_Angeles

## Security

### Pod Security
- Runs as non-root user (UID 1000, GID 1000)
- FSGroup set to 1000 for volume permissions
- All capabilities dropped

### Network Policy
- **Ingress**: Allows traffic from Traefik ingress controller on port 8787
- **Egress**: 
  - DNS (port 53)
  - HTTPS (ports 80, 443) for general internet access
  - rreading-glasses service (port 8080) within media namespace

## Resource Limits

- **Requests**: 100m CPU, 256Mi memory
- **Limits**: 1000m CPU, 1Gi memory

## Access

Once deployed, access Bookshelf at: https://bookshelf.homelab

Initial setup will guide you through:
1. Creating admin user
2. Configuring book root path: `/media/books`
3. Connecting to rreading-glasses metadata service

## Integration with rreading-glasses

Bookshelf connects to rreading-glasses for enhanced metadata:
- Service endpoint: `http://rreading-glasses:8080`
- Configure in Bookshelf Settings → Metadata

## Troubleshooting

### Check pod status
```bash
kubectl get pods -n media -l app.kubernetes.io/name=bookshelf
```

### View logs
```bash
kubectl logs -n media -l app.kubernetes.io/name=bookshelf
```

### Verify network connectivity to rreading-glasses
```bash
kubectl exec -n media -it <bookshelf-pod> -- wget -O- http://rreading-glasses:8080/health
```

### Check PVC
```bash
kubectl get pvc -n media bookshelf-config
```

### Verify media mount
```bash
kubectl exec -n media -it <bookshelf-pod> -- ls -la /media/books
```

## Notes

- Bookshelf requires the books directory to exist at `/media/books` on the host
- The media mount is read-write to allow Bookshelf to organize and manage files
- Initial configuration may take a few minutes as Bookshelf indexes existing books
