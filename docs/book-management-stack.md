# Book Management Stack

Complete production-quality book management ecosystem for personal library automation, reading, and Kindle delivery.

## Overview

The book management stack provides end-to-end automation for managing an eBook library:

1. **Bookshelf** - Acquisition and organization (Readarr fork)
2. **rreading-glasses** - Enhanced metadata via Hardcover.app
3. **Calibre-Web** - Web-based reading and browsing
4. **Kindle Sender** - Automatic delivery to Kindle devices
5. **Hardcover Sync** - Automatic sync from Hardcover "Want-To-Read" to Bookshelf

All components are deployed in the `media` namespace on `rishik-worker1` node.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                         User Access                                  │
├─────────────────┬────────────────────────┬───────────────────────────┤
│  https://       │  https://              │  Hardcover.app           │
│  bookshelf.     │  calibre.homelab       │  (Want-To-Read)          │
│  homelab        │                        │                          │
└────────┬────────┴───────────┬────────────┴───────────┬──────────────┘
         │                    │                        │
    ┌────▼────────┐     ┌────▼─────────┐      ┌───────▼────────┐
    │  Bookshelf  │────▶│ rreading-    │◀─────│   Hardcover    │
    │             │     │  glasses     │      │     Sync       │
    │  (8787)     │     │              │      │                │
    │             │     │  (8080)      │      │  (background)  │
    └─────┬───────┘     └──────┬───────┘      └────────────────┘
          │                    │                         
          │                ┌───▼────────┐               
          │                │ PostgreSQL │               
          │                │   (5432)   │               
          │                └────────────┘               
          │                                   ┌──────────────┐
          │     ┌────────────┐                │   Kindle     │
          └────▶│  Calibre-  │                │   Sender     │
                │   Web      │                │              │
                │  (8083)    │                │ (background) │
                └─────┬──────┘                └──────┬───────┘
                      │                              │
              ┌───────▼──────────────────────────────▼─┐
              │           /media/books                 │
              │                                        │
              │         calibre-library/               │
              └────────────────────────────────────────┘
```

## Components

### 1. Bookshelf

**Purpose**: Main book management interface

- **Type**: Readarr fork optimized for books
- **Access**: https://bookshelf.homelab
- **Port**: 8787
- **Storage**: 1Gi config PVC + hostPath media mount
- **Resources**: 100m-1000m CPU, 256Mi-1Gi memory

**Features**:
- Book search and acquisition
- Metadata enrichment via rreading-glasses
- Library organization and tracking
- Author and series management

**Configuration**:
- Books root: `/media/books`
- Metadata service: `http://rreading-glasses:8080`

### 2. rreading-glasses

**Purpose**: Metadata backend service

- **Type**: API service with PostgreSQL
- **Access**: Internal only (ClusterIP)
- **Port**: 8080
- **Storage**: 8Gi PostgreSQL PVC
- **Resources**: 50m-200m CPU, 128Mi-256Mi memory

**Features**:
- Hardcover.app API integration
- Metadata caching in PostgreSQL
- Enhanced book information
- Cover art and descriptions

**Database**:
- PostgreSQL 16-alpine
- Database: `rreading_glasses`
- User: `rreading_user`

### 3. Calibre-Web

**Purpose**: Web-based library browser

- **Type**: Web application
- **Access**: https://calibre.homelab
- **Port**: 8083
- **Storage**: 1Gi config PVC + hostPath media (READ-ONLY)
- **Resources**: 100m-500m CPU, 256Mi-512Mi memory

**Features**:
- Online reading (EPUB, PDF, etc.)
- Book search and filtering
- Manual email to Kindle
- User management
- Metadata editing

**Library Path**: `/media/books/calibre-library`

### 4. Kindle Sender

**Purpose**: Automatic Kindle delivery

- **Type**: Custom Go microservice
- **Access**: None (background service)
- **Storage**: 100Mi SQLite PVC + hostPath media (READ-ONLY)
- **Resources**: 25m-100m CPU, 32Mi-64Mi memory

**Features**:
- Real-time file monitoring (fsnotify)
- Periodic scanning (5 minutes)
- Duplicate prevention via SQLite
- Automatic SMTP email delivery
- Support: .epub, .mobi, .azw3, .pdf
- Max size: 50MB

**Source Code**: Available in `apps/kindle-sender/src/`

### 5. Hardcover Sync

**Purpose**: Automatic synchronization from Hardcover "Want-To-Read" list

- **Type**: Custom Go microservice
- **Access**: None (background service)
- **Storage**: 100Mi SQLite PVC for sync tracking
- **Resources**: 25m-100m CPU, 32Mi-64Mi memory

**Features**:
- Periodic sync from Hardcover.app (default: 1 hour)
- GraphQL API integration with Hardcover
- Metadata resolution via rreading-glasses
- Automatic book addition to Bookshelf
- Duplicate prevention via SQLite tracking
- Graceful error handling

**Workflow**:
1. Query Hardcover GraphQL API for "Want-To-Read" books (status_id = 1)
2. For each book ID, call rreading-glasses: `GET /works/<hardcover-id>`
3. Add resolved metadata to Bookshelf via `POST /api/v1/book`
4. Track synced books in SQLite to prevent duplicates

**Source Code**: Available in `apps/hardcover-sync/src/`

## Deployment Prerequisites

### 1. Host System Setup

Create the books directory structure:

```bash
# Create main books directory
sudo mkdir -p /media/rishik/Expansion/books
sudo chown 1000:1000 /media/rishik/Expansion/books

# Initialize Calibre library (optional, can be done via Calibre-Web UI)
# Note: Library can also be initialized through Calibre-Web's first-run setup
sudo mkdir -p /media/rishik/Expansion/books/calibre-library
```

### 2. Hardcover API Key

1. Sign up at https://hardcover.app
2. Go to Settings → API
3. Generate an API key
4. Save for sealed secret creation

### 3. Kindle Configuration

1. Find your Kindle email:
   - Amazon Account → Content & Devices → Preferences
   - Personal Document Settings
   - Note your "Send-to-Kindle Email Address"

2. Add approved sender:
   - In Personal Document Settings
   - "Approved Personal Document E-mail List"
   - Add your SMTP sender email

### 4. SMTP Setup (Gmail Example)

1. Enable 2-factor authentication on Google account
2. Generate App Password:
   - https://myaccount.google.com/apppasswords
   - Select "Mail" and your device
   - Copy the 16-character password
3. Configuration:
   - SMTP_HOST: `smtp.gmail.com`
   - SMTP_PORT: `587`
   - SMTP_USER: `your-email@gmail.com`
   - SMTP_PASSWORD: `<app-password>`

## Sealed Secrets

### rreading-glasses Credentials

```bash
POSTGRES_PASSWORD="$(openssl rand -base64 32)"

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

### Calibre-Web SMTP Credentials

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

### Kindle Sender Credentials

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

### Hardcover Sync Credentials

```bash
kubectl create secret generic hardcover-sync-credentials \
  --namespace=media \
  --from-literal=HARDCOVER_API_KEY="your-hardcover-api-key" \
  --from-literal=BOOKSHELF_API_KEY="your-bookshelf-api-key" \
  --dry-run=client -o yaml | \
kubeseal --format yaml \
  --controller-name=sealed-secrets \
  --controller-namespace=flux-system \
  > apps/hardcover-sync/sealedsecret-hardcover-sync-credentials.yaml
```

## Deployment

### Via Flux GitOps

1. Seal all secrets (see above)
2. Commit and push changes:
   ```bash
   git add apps/bookshelf apps/rreading-glasses apps/calibre-web apps/kindle-sender apps/hardcover-sync
   git add apps/kustomization.yaml
   git commit -m "Add book management stack"
   git push
   ```
3. Flux will automatically deploy within 10 minutes

### Manual Deployment

```bash
# Deploy all components
kubectl apply -k apps/bookshelf/
kubectl apply -k apps/rreading-glasses/
kubectl apply -k apps/calibre-web/
kubectl apply -k apps/kindle-sender/
kubectl apply -k apps/hardcover-sync/
```

## Post-Deployment Configuration

### Bookshelf Setup

1. Access https://bookshelf.homelab
2. Complete initial setup wizard
3. Configure root folder: `/media/books`
4. Add metadata service:
   - Settings → Metadata
   - Custom Metadata Source
   - URL: `http://rreading-glasses:8080`

### Calibre-Web Setup

1. Access https://calibre.homelab
2. Login with default credentials: `admin` / `admin123`
3. **IMPORTANT**: Change admin password immediately
4. Set Calibre library path: `/media/books/calibre-library`
5. Configure email server for Send-to-Kindle:
   - Navigate to **Admin → Email Server Settings**
   - Enter SMTP server details (hostname, port, credentials)
   - Enable SSL/TLS as required
   - Test email delivery

## Verification

### Check Pod Status

```bash
# All book stack pods
kubectl get pods -n media -l 'app.kubernetes.io/name in (bookshelf,rreading-glasses,calibre-web,kindle-sender,hardcover-sync)'

# Individual components
kubectl get pods -n media -l app.kubernetes.io/name=bookshelf
kubectl get pods -n media -l app.kubernetes.io/name=rreading-glasses
kubectl get pods -n media -l app.kubernetes.io/name=calibre-web
kubectl get pods -n media -l app.kubernetes.io/name=kindle-sender
kubectl get pods -n media -l app.kubernetes.io/name=hardcover-sync
```

### Check Services

```bash
kubectl get svc -n media | grep -E "(bookshelf|rreading|calibre|kindle|hardcover)"
```

### Check Ingresses

```bash
kubectl get ingress -n media | grep -E "(bookshelf|calibre)"
```

### View Logs

```bash
# Bookshelf
kubectl logs -n media -l app.kubernetes.io/name=bookshelf -f

# rreading-glasses
kubectl logs -n media -l app.kubernetes.io/name=rreading-glasses -f

# Calibre-Web
kubectl logs -n media -l app.kubernetes.io/name=calibre-web -f

# Kindle Sender
kubectl logs -n media -l app.kubernetes.io/name=kindle-sender -f

# Hardcover Sync
kubectl logs -n media -l app.kubernetes.io/name=hardcover-sync -f
```

## Workflow

### Manual Book Management
1. **Book Discovery**: Use Bookshelf to search and add books
2. **Acquisition**: Bookshelf downloads books to `/media/books/`
3. **Metadata**: Bookshelf enriches metadata via rreading-glasses
4. **Organization**: Books are organized in Calibre library format
5. **Browsing**: Access via Calibre-Web at https://calibre.homelab
6. **Reading**: Read online via Calibre-Web interface
7. **Delivery**: Kindle Sender automatically emails new books to Kindle

### Automated Book Management (via Hardcover Sync)
1. **Discovery on Hardcover**: Browse and add books to "Want-To-Read" on Hardcover.app
2. **Automatic Sync**: Hardcover Sync periodically picks up new additions (hourly)
3. **Metadata Resolution**: rreading-glasses enhances book information
4. **Bookshelf Addition**: Books are automatically added to Bookshelf
5. **Acquisition**: Bookshelf handles downloading and organizing
6. **Browsing**: Access via Calibre-Web at https://calibre.homelab
7. **Reading**: Read online via Calibre-Web interface
8. **Delivery**: Kindle Sender automatically emails new books to Kindle

## Troubleshooting

### Bookshelf Issues

**Cannot connect to rreading-glasses**:
```bash
# Check rreading-glasses is running
kubectl get pods -n media -l app.kubernetes.io/name=rreading-glasses

# Test connectivity from Bookshelf pod
kubectl exec -n media -it <bookshelf-pod> -- wget -O- http://rreading-glasses:8080/health
```

**Permission denied on /media/books**:
```bash
# Verify ownership on host
ls -la /media/rishik/Expansion/books/

# Should be owned by UID 1000
sudo chown -R 1000:1000 /media/rishik/Expansion/books/
```

### rreading-glasses Issues

**PostgreSQL connection failed**:
```bash
# Check PostgreSQL pod
kubectl get pods -n media -l app.kubernetes.io/instance=rreading-glasses-postgresql

# Check PostgreSQL logs
kubectl logs -n media -l app.kubernetes.io/instance=rreading-glasses-postgresql

# Verify secret exists
kubectl get secret rreading-glasses-credentials -n media
```

**Hardcover API errors**:
- Verify API key is correct in sealed secret
- Check rate limits on Hardcover.app account

### Calibre-Web Issues

**Cannot find Calibre library**:
```bash
# Verify library exists
kubectl exec -n media -it <calibre-web-pod> -- ls -la /media/books/calibre-library/

# Should contain metadata.db
kubectl exec -n media -it <calibre-web-pod> -- ls /media/books/calibre-library/metadata.db
```

**Email delivery fails**:
- Verify SMTP credentials in sealed secret
- Check sender email is approved in Amazon Kindle settings
- Test SMTP connection from pod

### Kindle Sender Issues

**Files not being sent**:
```bash
# Check logs for errors
kubectl logs -n media -l app.kubernetes.io/name=kindle-sender -f

# Verify watch path has files
kubectl exec -n media -it <kindle-sender-pod> -- ls -la /media/books/

# Check database
kubectl exec -n media -it <kindle-sender-pod> -- ls -la /data/
```

**SMTP errors**:
- Same troubleshooting as Calibre-Web email issues
- Check network policy allows SMTP egress
- Verify SMTP port (587 vs 465 vs 25)

## Security

### Network Policies

All components have strict network policies:

- **Bookshelf**: Ingress from Traefik only, egress to rreading-glasses and HTTPS
- **rreading-glasses**: Ingress from Bookshelf only, egress to PostgreSQL and HTTPS
- **Calibre-Web**: Ingress from Traefik only, egress to SMTP and HTTPS
- **Kindle Sender**: No ingress, egress to SMTP and DNS only

### Pod Security

All pods run with:
- Non-root user (UID 1000)
- Read-only root filesystem (where possible)
- No privilege escalation
- All capabilities dropped
- Resource limits enforced

### Secret Management

All secrets are sealed using Bitnami Sealed Secrets:
- Encrypted at rest in Git
- Decrypted only in-cluster by sealed-secrets controller
- Never stored in plain text

## Monitoring

### Prometheus Metrics

Components are monitored via Prometheus:
- Pod status and restarts
- Resource usage (CPU, memory)
- Network traffic

### Grafana Dashboards

View metrics in Grafana:
- https://grafana.homelab
- Dashboard: "Applications" or "Kubernetes / Pods"

### Alerting

Configure Alertmanager alerts for:
- Pod restarts
- High resource usage
- Persistent volume capacity

## Maintenance

### Backups

**PVCs to backup**:
- `bookshelf-config` (1Gi) - Bookshelf configuration
- `rreading-glasses-postgresql-data` (8Gi) - Metadata cache
- `calibre-web-config` (1Gi) - Calibre-Web settings
- `kindle-sender-data` (100Mi) - Sent files database
- `hardcover-sync-data` (100Mi) - Synced books tracking database

**Longhorn snapshots**:
```bash
# Create snapshot via Longhorn UI or kubectl
kubectl annotate pvc bookshelf-config -n media snapshot.longhorn.io/create=backup-$(date +%Y%m%d)
```

### Updates

**Application updates**:
- Update image tag in helmrelease.yaml
- Commit and push (Flux will apply)

**Database migrations**:
- rreading-glasses handles migrations automatically
- PostgreSQL version updates should be tested first

## Integration with Existing Media Stack

The book management stack integrates seamlessly with existing media apps:

- **Shared Media**: All apps use `/media/rishik/Expansion`
- **Same Node**: Pinned to `rishik-worker1` like other media apps
- **Consistent Patterns**: Same bjw-s app-template and security practices
- **Namespace**: All in `media` namespace with existing apps

## Cost Analysis

### Resource Usage

Total resource allocation:

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Bookshelf | 100m | 1000m | 256Mi | 1Gi |
| rreading-glasses | 50m | 200m | 128Mi | 256Mi |
| PostgreSQL | 50m | 200m | 128Mi | 256Mi |
| Calibre-Web | 100m | 500m | 256Mi | 512Mi |
| Kindle Sender | 25m | 100m | 32Mi | 64Mi |
| Hardcover Sync | 25m | 100m | 32Mi | 64Mi |
| **Total** | **350m** | **2100m** | **832Mi** | **2.12Gi** |

### Storage Usage

Total storage allocation:
- bookshelf-config: 1Gi
- rreading-glasses-postgresql-data: 8Gi
- calibre-web-config: 1Gi
- kindle-sender-data: 100Mi
- hardcover-sync-data: 100Mi
- **Total PVC**: ~10.2Gi

Books themselves are stored on hostPath (external HDD).

## Future Enhancements

Possible improvements:

1. **LazyLibrarian Integration**: Alternative/additional book manager
2. **Audiobookshelf**: Audiobook management and streaming
3. **Komga**: Comic book server
4. **Metrics Dashboard**: Custom Grafana dashboard for book stack
5. **Backup Automation**: Automated PVC snapshots via CronJob
6. **Book Recommendations**: ML-based recommendation engine
7. **Readarr Sync**: Sync with original Readarr for compatibility

## References

- [Bookshelf GitHub](https://github.com/pennydreadful/bookshelf)
- [rreading-glasses GitHub](https://github.com/blampe/rreading-glasses)
- [Calibre-Web GitHub](https://github.com/janeczku/calibre-web)
- [Hardcover.app](https://hardcover.app)
- [bjw-s app-template](https://github.com/bjw-s/helm-charts)
- [Sealed Secrets](https://sealed-secrets.netlify.app/)
