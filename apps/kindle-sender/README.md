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

The Go application is located in `src/` directory:

```bash
cd apps/kindle-sender/src
docker build -t ghcr.io/blazepower/kindle-sender:v1.0.0 .
docker push ghcr.io/blazepower/kindle-sender:v1.0.0
```

### Multi-stage Build
The Dockerfile uses a multi-stage build:
1. **Builder stage**: Compiles Go application with static linking
2. **Final stage**: Minimal Alpine image with CA certificates

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
