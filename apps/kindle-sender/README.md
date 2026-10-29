# Kindle Sender

Kindle Sender is a custom Go microservice that automatically watches for new eBooks and sends them to your Kindle device via email.

## Overview

- **Image**: `ghcr.io/blazepower/kindle-sender:v1.0.0`
- **Namespace**: media
- **Node**: rishik-worker1
- **Watch Path**: `/media/books`

## Architecture

Kindle Sender is a lightweight Go application that:
1. Watches `/media/books` directory for new eBook files
2. Performs periodic scans every 5 minutes (configurable)
3. Tracks sent files in a SQLite database to prevent duplicates
4. Automatically emails new books to your Kindle device via SMTP

## Features

### Supported File Formats
- `.epub` - EPUB format
- `.mobi` - MOBI format
- `.azw3` - AZW3 format
- `.pdf` - PDF format

### File Watching
- Real-time file system monitoring using fsnotify
- Periodic scanning as backup (default: 300 seconds / 5 minutes)
- Duplicate detection via SQLite database

### Size Limits
- Maximum file size: 50MB (configurable)
- Files exceeding the limit are skipped and logged

## Storage

- **Data PVC**: 100Mi Longhorn volume for SQLite state database
- **Media Mount**: hostPath mount of `/media/rishik/Expansion` to `/media` (READ-ONLY)
  - Books directory: `/media/books`

## Configuration

### Environment Variables (via ConfigMap)

- `WATCH_PATH`: Directory to watch for new books (default: `/media/books`)
- `SCAN_INTERVAL`: Seconds between periodic scans (default: `300`)
- `MAX_FILE_SIZE_MB`: Maximum file size in MB (default: `50`)
- `FILE_EXTENSIONS`: Comma-separated list of supported extensions (default: `.epub,.mobi,.azw3,.pdf`)
- `DATABASE_PATH`: SQLite database location (default: `/data/kindle-sender.db`)

### SMTP Configuration (via SealedSecret)

Required secrets:
- `SMTP_HOST`: SMTP server hostname
- `SMTP_PORT`: SMTP server port
- `SMTP_USER`: SMTP authentication username
- `SMTP_PASSWORD`: SMTP authentication password
- `KINDLE_EMAIL`: Your Kindle email address
- `SENDER_EMAIL`: Email address to use as sender

## Sealing Secrets

To create and seal the SMTP credentials:

```bash
kubectl create secret generic kindle-sender \
  --namespace=media \
  --from-literal=SMTP_HOST="smtp.gmail.com" \
  --from-literal=SMTP_PORT="587" \
  --from-literal=SMTP_USER="your-email@gmail.com" \
  --from-literal=SMTP_PASSWORD="your-app-password" \
  --from-literal=KINDLE_EMAIL="your-kindle@kindle.com" \
  --from-literal=SENDER_EMAIL="your-email@gmail.com" \
  --dry-run=client -o yaml | \
kubeseal --format yaml \
  --controller-name=sealed-secrets \
  --controller-namespace=flux-system \
  > apps/kindle-sender/sealedsecret-kindle-sender.yaml
```

### Gmail Setup

If using Gmail:
1. Enable 2-factor authentication
2. Generate an App Password at https://myaccount.google.com/apppasswords
3. Use the app password as `SMTP_PASSWORD`
4. Use `smtp.gmail.com` as `SMTP_HOST` and `587` as `SMTP_PORT`

### Kindle Setup

1. Find your Kindle email:
   - Go to Amazon Account → Content & Devices → Preferences
   - Look for "Personal Document Settings"
   - Note your "Send-to-Kindle Email Address"

2. Add sender email to approved list:
   - In Personal Document Settings
   - Under "Approved Personal Document E-mail List"
   - Add your `SENDER_EMAIL` address

3. Configure the service:
   - Use the Kindle email as `KINDLE_EMAIL`

## Security

### Pod Security
- Runs as non-root user (UID 1000, GID 1000)
- FSGroup set to 1000 for volume permissions
- Read-only root filesystem
- All capabilities dropped
- Media mount is READ-ONLY

### Network Policy
- **No Ingress**: This service doesn't accept incoming connections
- **Egress**: 
  - DNS (port 53)
  - SMTP (ports 25, 465, 587) for email delivery

## Resource Limits

- **Requests**: 25m CPU, 32Mi memory
- **Limits**: 100m CPU, 64Mi memory

Kindle Sender is designed to be extremely lightweight and efficient.

## How It Works

### Initial Scan
1. On startup, performs a full scan of the watch directory
2. Checks each supported file against the SQLite database
3. Sends any new files to Kindle email

### Ongoing Monitoring
1. **File Watcher**: Uses fsnotify to detect new files immediately
2. **Periodic Scan**: Runs every 5 minutes as backup (in case watcher missed events)
3. **Duplicate Prevention**: SQLite database tracks sent files by path

### Email Delivery
1. Reads the eBook file
2. Creates MIME multipart email with attachment
3. Sends via SMTP to Kindle email
4. Marks file as sent in database

## Building the Docker Image

The maintained application source is in `src/`, not a separate repository inferred
from the GHCR username. The current immutable `v1.0.0` amd64 artifact
(`sha256:4ecbbe85df4075286e3c5b2bab081abbd9b784e23856258b586d33e860754177`)
has no OCI source/revision labels and its native build-info reports Go 1.21.13.
Changing a CA bundle alone cannot patch that compiled runtime.

From the repository root, on a Linux Docker/Buildx runner with Python, OpenSSL,
and Go available, run the **non-publishing** acceptance command:

```bash
python3 .github/scripts/test-kindle-ca-build.py
python3 .github/scripts/verify-kindle-ca.py --output .kindle-ca-proof.json
```

### Multi-stage Build
The Dockerfile pins Go 1.27.3 and the distroless seed by immutable digest, keeps
`GOTOOLCHAIN=local`, and retains the application version and maintained dependency
manifest. The existing protobuf 1.33.0 security update is preserved; the old
artifact used 1.31.0, so its dependency graph is not claimed to be byte-identical.
No application behavior or database format is changed.

The named build context `kindle-policy=.github/security` supplies the canonical
eleven-SPKI policy without sending real application configuration as build
context. A build-only Go tool removes exact matches before regenerating **all**
system trust paths. The final image uses the distroless filesystem plus those
cleared roots, a statically linked CGO/SQLite binary, and UID/GID 65532.

Acceptance compares the actual production and test-image binaries and bundles,
checks native application build-info and every module version against `go.mod`,
checks the exact public-root SPKIs, and executes a native in-memory SQLite query
and the same-toolchain native trust tests
on the actual final filesystem with no outbound network, read-only root,
no capabilities, and no application entrypoint. Tests never read real config,
book data, SMTP credentials, or send messages. The test binary is absent from
the production target.

All eight compromised public SPKIs in `src/kindle-ca-spkis.json` were extracted
from public certificates at
`https://ctlogs.dev/ca/<certificate_sha256>/cert.pem`, with each certificate's DER
SHA-256 verified against the incident disclosure linked in the canonical policy.
The tests verify each SPKI against that policy, issue synthetic test certificates
under an ephemeral local CA, prove native chain-policy rejection and real
loopback TLS rejection, and include valid-TLS and ordinary unknown-root controls.
No compromised private key is used: a negative TLS case must specifically fail
**certificate validation**, not merely fail a subsequent handshake signature.

The Kindle-only workflow records proof in its job summary. Local Windows native
tests are not Linux image acceptance; the proof JSON is emitted only after the
complete Linux acceptance succeeds. Publication and a separately reviewed
immutable HelmRelease update remain gated on that evidence. This preparation
does not publish, push, deploy, reconcile Flux, or modify cluster storage.

## Troubleshooting

### Check pod status
```bash
kubectl get pods -n media -l app.kubernetes.io/name=kindle-sender
```

### View logs
```bash
kubectl logs -n media -l app.kubernetes.io/name=kindle-sender -f
```

### Verify configuration
```bash
kubectl exec -n media -it <kindle-sender-pod> -- env | grep -E '(WATCH|SMTP|KINDLE)'
```

### Check database
```bash
kubectl exec -n media -it <kindle-sender-pod> -- ls -la /data/
```

### Verify media mount
```bash
kubectl exec -n media -it <kindle-sender-pod> -- ls -la /media/books/
```

### Check sent files database
```bash
# The database is in /data/kindle-sender.db
# You can view it from a shell in the pod
kubectl exec -n media -it <kindle-sender-pod> -- sh
# Then manually inspect the SQLite database if needed
```

### Test SMTP Connection
Check logs for SMTP connection errors:
```bash
kubectl logs -n media -l app.kubernetes.io/name=kindle-sender | grep -i smtp
```

### Common Issues

#### Files not being sent
1. Check file format is supported (`.epub`, `.mobi`, `.azw3`, `.pdf`)
2. Verify file size is under 50MB
3. Check if file was already sent (database records)
4. Verify watch path contains files: `/media/books/`

#### SMTP errors
1. Verify SMTP credentials in sealed secret
2. Check if sender email is approved in Kindle settings
3. Test SMTP connection from pod
4. Check network policy allows SMTP egress

#### Permission errors
1. Verify media mount is accessible
2. Check `/data` directory is writable
3. Verify pod runs as UID 1000

## Notes

- Media mount is read-only to prevent accidental modifications
- SQLite database persists across pod restarts
- Files are sent automatically - no manual intervention needed
- Duplicate files (by path) are never sent twice
- The service is designed to run continuously
- Initial scan may take time depending on library size

## Integration

### With Bookshelf
- Bookshelf downloads and organizes books to `/media/books/`
- Kindle Sender automatically detects and sends new arrivals

### With Calibre-Web
- Both services can coexist
- Calibre-Web provides manual send option
- Kindle Sender provides automatic send functionality

## Development

The source code is in `apps/kindle-sender/src/`:
- `main.go`: Main application logic
- `go.mod`: Go module dependencies
- `Dockerfile`: Container build instructions

Dependencies:
- `github.com/fsnotify/fsnotify`: File system monitoring
- `github.com/mattn/go-sqlite3`: SQLite database driver
