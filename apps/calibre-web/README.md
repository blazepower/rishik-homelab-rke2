# Calibre-Web

Calibre-Web is a web-based eBook library browser that provides a clean interface for browsing, reading, and managing your Calibre library.

## Overview

- **Image**: `lscr.io/linuxserver/calibre-web:0.6.24`
- **Port**: 8083
- **Access URL**: https://calibre.homelab
- **Namespace**: media
- **Node**: rishik-worker1

## Architecture

Calibre-Web provides:
- Web interface for browsing Calibre libraries
- Online eBook reading (EPUB, PDF, etc.)
- Email delivery to Kindle devices
- User management and book organization
- Metadata editing and search

## Storage

- **Config PVC**: 1Gi Longhorn volume for application configuration and database
- **Media Mount**: hostPath mount of `/media/rishik/Expansion` to `/media` (READ-ONLY)
  - Calibre library path: `/media/books/calibre-library`

## Configuration

### Environment Variables (via ConfigMap)
- `PUID`: 1000
- `PGID`: 1000
- `TZ`: America/Los_Angeles

### SMTP Configuration

**Note**: The LinuxServer.io Calibre-Web image does not support SMTP configuration via environment variables. SMTP must be configured manually through the web UI after deployment.

For Kindle email delivery, you'll need the following information:
- `SMTP_HOST`: SMTP server hostname (e.g., smtp.gmail.com)
- `SMTP_PORT`: SMTP server port (587 for STARTTLS, 465 for TLS/SSL)
- `SMTP_USER`: SMTP authentication username
- `SMTP_PASSWORD`: SMTP authentication password
- `KINDLE_EMAIL`: Your Kindle email address

## Initial Setup

1. Access https://calibre.homelab
2. Default credentials: `admin` / `admin123` (change immediately!)
3. Set Calibre library path: `/media/books/calibre-library`
4. Configure email settings for Kindle delivery via **Admin > Email Server** in the web UI

## SMTP Setup

**Important**: SMTP credentials must be entered manually through the Calibre-Web web UI. The sealed secret file (`sealedsecret-calibre-smtp.yaml`) is provided as a template but is not used by the application.

To configure email for Send-to-Kindle:

1. Access https://calibre.homelab and login as admin
2. Navigate to **Admin > Email Server Settings**
3. Enter your SMTP configuration:
   - SMTP hostname: e.g., smtp.gmail.com
   - SMTP port: 587 (STARTTLS) or 465 (SSL/TLS)
   - From email: Your email address
   - SMTP login: Your SMTP username
   - SMTP password: Your SMTP password
4. Enable "Use SSL/TLS"
5. Save settings
6. Test by sending a book to your Kindle email

## Alternative: If Using Environment Variables (Future Enhancement)

If you need to manage SMTP credentials as Kubernetes secrets for reference purposes:

```bash
kubectl create secret generic calibre-smtp \
  --namespace=media \
  --from-literal=SMTP_HOST="smtp.gmail.com" \
  --from-literal=SMTP_PORT="587" \
  --from-literal=SMTP_USER="your-email@gmail.com" \
  --from-literal=SMTP_PASSWORD="your-app-password" \
  --from-literal=KINDLE_EMAIL="your-kindle@kindle.com" \
  --dry-run=client -o yaml | \
kubeseal --format yaml \
  --controller-name=sealed-secrets \
  --controller-namespace=flux-system \
  > apps/calibre-web/sealedsecret-calibre-smtp.yaml
```

### Gmail Setup

If using Gmail:
1. Enable 2-factor authentication
2. Generate an App Password at https://myaccount.google.com/apppasswords
3. Use the app password as `SMTP_PASSWORD`

### Kindle Setup

1. Find your Kindle email: Amazon Account → Content & Devices → Preferences → Personal Document Settings
2. Add your SMTP sender email to approved senders list
3. Use the Kindle email as `KINDLE_EMAIL`

## Security

### Pod Security
- Runs as non-root user (UID 1000, GID 1000)
- FSGroup set to 1000 for volume permissions
- All capabilities dropped
- Media mount is READ-ONLY to prevent accidental library modifications

### Network Policy
- **Ingress**: Allows traffic from Traefik ingress controller on port 8083
- **Egress**: 
  - DNS (port 53)
  - SMTP (ports 587, 465) for email delivery
  - HTTPS (ports 80, 443) for metadata and covers

## Resource Limits

- **Requests**: 100m CPU, 256Mi memory
- **Limits**: 500m CPU, 512Mi memory

## Features

### Supported Formats
- EPUB (read online)
- PDF (read online)
- MOBI, AZW3 (download/email to Kindle)
- CBR, CBZ (comic books)
- And many more...

### Email to Kindle
Configure in Admin → Settings → E-Mail:
1. Set SMTP server details (from sealed secret)
2. Test email delivery
3. Users can email books directly to their Kindle from book detail pages

### User Management
- Create multiple users with different permissions
- Set reading progress per user
- Configure per-user email addresses

## Troubleshooting

### Check pod status
```bash
kubectl get pods -n media -l app.kubernetes.io/name=calibre-web
```

### View logs
```bash
kubectl logs -n media -l app.kubernetes.io/name=calibre-web
```

### Verify Calibre library path
```bash
kubectl exec -n media -it <calibre-web-pod> -- ls -la /media/books/calibre-library
```

### Test SMTP configuration
- Open the Calibre-Web web UI and navigate to **Admin > Email Server Settings**
- Verify that SMTP settings (server, port, username, password, TLS/SSL) are correctly entered
- Use the "Test email" feature in the UI to verify SMTP connectivity
- If email fails, check pod logs for SMTP-related errors:
  ```bash
  kubectl logs -n media -l app.kubernetes.io/name=calibre-web | grep -i mail
  ```

### Check PVC
```bash
kubectl get pvc -n media calibre-web-config
```

### Cannot find Calibre library
Ensure the library exists at `/media/rishik/Expansion/books/calibre-library` on the host and contains:
- `metadata.db` file
- Book directories

If library doesn't exist, you can:
- Create one via Calibre-Web's first-run setup (easiest method)
- Or manually create directory: `sudo mkdir -p /media/rishik/Expansion/books/calibre-library && sudo chown 1000:1000 /media/rishik/Expansion/books/calibre-library`

## Integration

### With Bookshelf
- Bookshelf manages book acquisition and organization
- Calibre-Web provides the reading and browsing interface
- Books organized by Bookshelf appear in Calibre-Web

### With Kindle Sender
- Manual email delivery via Calibre-Web UI
- Automatic delivery via Kindle Sender service

## Notes

- Media mount is read-only to prevent accidental modifications to the Calibre library
- Calibre-Web uses its own database for user data, reading progress, etc.
- The application respects the Calibre library structure and metadata.db
- For email delivery to work, SMTP configuration must be completed
- Initial setup requires setting the Calibre library location only once

## Access

Once deployed, access Calibre-Web at: https://calibre.homelab

Default credentials (CHANGE IMMEDIATELY):
- Username: `admin`
- Password: `admin123`
