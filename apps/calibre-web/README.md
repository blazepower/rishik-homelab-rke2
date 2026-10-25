# Calibre-Web

Calibre-Web is a web-based eBook library browser that provides a clean interface for browsing, reading, and managing your Calibre library.

## Overview

- **Image**: `lscr.io/linuxserver/calibre-web:0.6.27` (digest pinned in the HelmRelease)
- **Port**: 8083
- **Access URL**: https://calibre.homelab
- **Namespace**: media
- **Scheduling**: One always-on replica, scheduled on an available worker; Recreate updates

## Architecture

Calibre-Web provides:
- Web interface for browsing Calibre libraries
- Online eBook reading (EPUB, PDF, etc.)
- Email delivery to Kindle devices
- User management and book organization
- Metadata editing and search

## Storage

- **Config PVC**: `calibre-web-config`, 1Gi Longhorn, mounted at `/config`.
- **Library PVC**: `calibre-web-library`, 2Gi RWO `longhorn-retain`, mounted
  read-write at `/media/books/calibre-library`. The Calibre SQLite database and
  book files stay together on this filesystem, not NFS.
- **Legacy source**: `media-root` NFS `books/calibre-library`, mounted read-only
  at `/legacy-library` in the bootstrap init container only. Other applications
  retain their existing NFS mounts.
- Browser uploads go directly through Calibre-Web; no ingest directory, desktop
  sync, or second library/progress-sync service is required.

### First Longhorn cutover

Before approving the GitOps rollout:

1. Confirm the configured library is `/media/books/calibre-library`, its current
   database is valid and empty, and no external process or Syncthing folder is
   writing/replacing that database. If any writer is active, postpone the cutover;
   do not change other services as part of this migration.
2. Capture the existing pod status, logs, events, and mount/PVC status. Verify
   `longhorn-retain` exists and Flux dependencies are healthy before rollout.
3. Keep the old NFS content intact. Recreate updates stop the old Calibre-Web
   before the new pod's init container runs. The init container opens the legacy
   database read-only and uses SQLite's backup API, not a file copy. It rejects
   legacy WAL/journal sidecars, a missing/corrupt database, or any books/formats.
4. The backup is validated and atomically installed as the new `metadata.db`.
   There is no downloaded/sample database or silent empty-library fallback.
   A failed bootstrap blocks startup; inspect `bootstrap-library` logs and
   resolve the cause rather than deleting the old database or bypassing checks.
   Existing destination databases are checked and never overwritten; SQLite can
   perform normal journal recovery on the writable destination before checking.
5. Verify `calibre-web-library` is Bound, the pod is Ready, and the init log says
   the verified empty library migrated:

   ```bash
   kubectl get pvc -n media calibre-web-config calibre-web-library
   kubectl rollout status -n media deployment/calibre-web --timeout=300s
   kubectl logs -n media deployment/calibre-web -c bootstrap-library
   ```

   Confirm the app still uses the same
   library path and that a subsequent restart verifies the existing Longhorn
   database without migrating again.

The read-only legacy mount remains a NAS startup dependency during this staged
cutover. After the new library and its backups are verified, a follow-up GitOps
change can remove that mount and the migration branch, retaining the destination
database check. Do not roll back to the old empty NFS library after uploading
books; restore the Longhorn library and configuration together instead.

### Uploads and device OPDS

After cutover, use the authenticated admin UI to change any default password,
keep anonymous browsing and public registration disabled, enable book uploading,
and grant upload permission only to the intended browser user. These application
settings persist in `/config`; this manifest does not change authentication.

Import the Windows library's book files through the browser, not its live
`metadata.db`. Convert MOBI files to EPUB in desktop Calibre first when needed.
Never share the managed library with desktop Calibre or a bidirectional sync
writer. Keep `/config`, the library, and any future `/ingest` mount separate.

Use `https://calibre.homelab/opds` where the device trusts the private CA, or the
LAN-only HTTP endpoint below. Verify Basic authentication, catalog browsing, and
EPUB download on both CrossPoint and KOReader before considering setup complete.
Existing private-CA/Tailscale ingress remains unchanged.
Keep hosted CrossPoint Sync for reading progress; OPDS only supplies books.

### LAN-only device access

The MetalLB service `calibre-web-lan` requests `192.168.1.153` from the existing
`homelab-pool` (`192.168.1.150-192.168.1.220`). Devices on `192.168.1.0/24` can
use **`http://192.168.1.153:8083/opds`** without private-CA support.
`externalTrafficPolicy: Local` preserves client IPs; the Service's source ranges
and the pod NetworkPolicy both restrict direct access to that LAN.

**Before deploying this endpoint**, change any default admin password through the
existing HTTPS UI, keep anonymous browsing/public registration off, and create a
unique, strong-password local account with download permission only. Do not grant
that account admin, upload, edit, or delete permissions. Verify unauthenticated
OPDS returns 401 and the device account cannot administer/upload before testing
downloads. Do not enable the endpoint until these prerequisites are satisfied.

HTTP Basic authentication is unencrypted on the LAN. Use a dedicated password
that is not reused anywhere else, and administer/upload through HTTPS rather
than logging in as admin over HTTP. The direct Service reaches the application,
not an OPDS-only proxy; application permissions remain essential. Do not add
router port forwarding, public DNS, Tailscale Funnel, or a public ingress for it.
Recheck that the requested address is unused before rollout.

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
3. Keep Calibre library path: `/media/books/calibre-library` (bootstrapped as above)
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
kubectl get pvc -n media calibre-web-config calibre-web-library
```

### Cannot find Calibre library
Ensure the library PVC mounted at `/media/books/calibre-library` contains:
- `metadata.db` file
- Book directories

If initialization fails, inspect the bootstrap logs:

```bash
kubectl logs -n media deployment/calibre-web -c bootstrap-library
```

Do not create an empty directory or replace a database to bypass the bootstrap
guard. A nonempty legacy library needs a separately planned, offline migration
of its database and all associated book files.

## Integration

### With Bookshelf
- Bookshelf manages book acquisition and organization
- Calibre-Web provides the reading and browsing interface
- Bookshelf retains its NFS library; import selected book files into Calibre-Web
  through the authenticated UI. It is not a writer of the Longhorn library.

### With Kindle Sender
- Manual email delivery via Calibre-Web UI
- Kindle Sender retains its existing NFS watch directory; new Calibre-Web uploads
  do not automatically appear there.

## Notes

- The managed library is writable by Calibre-Web only; its legacy NFS source is read-only.
- Calibre-Web uses a separate configuration database for users and settings.
- Hosted CrossPoint Sync remains the device reading-progress service.
- The application respects the Calibre library structure and metadata.db
- For email delivery to work, SMTP configuration must be completed
- Initial setup requires setting the Calibre library location only once

## Access

Once deployed, access Calibre-Web at: https://calibre.homelab

Default credentials (CHANGE IMMEDIATELY):
- Username: `admin`
- Password: `admin123`
