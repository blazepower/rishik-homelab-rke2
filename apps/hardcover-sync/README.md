# Hardcover Sync

Hardcover Sync is a custom Go microservice that automatically synchronizes books from your Hardcover "Want-To-Read" list to Bookshelf (Readarr fork) using rreading-glasses as the metadata resolution layer.

## Overview

- **Image**: `ghcr.io/blazepower/hardcover-sync` (digest-pinned in `cronjob.yaml`)
- **Namespace**: media
- **Node**: rishik-worker1
- **Type**: CronJob (runs every 2 hours)
- **Access**: None (background service)

## Owned image maintenance

The Hardcover Sync workflow always pulls its base
images. After the workflow changes are available on the selected ref, explicitly
request an uncached rebuild with:

```powershell
gh workflow run build-hardcover-sync.yaml --ref master -f force_rebuild=true
```

The workflow first publishes only a unique
`ca-candidate-<run-id>-<run-attempt>` quarantine tag, then checks that image's
system CA bundle on every built platform without starting the application.
After every check succeeds, the run summary reports `PASSED CANDIDATE` with
the verified immutable digest. Feature-branch dispatches stop there without
overwriting production tags. Promotion is limited to the repository's default
branch or a release-tag push. Those runs promote the verified digest to the
release/latest tags and then record the actual production `ghcr.io/...@sha256:...`
reference from promotion metadata.
The check uses `.github/security/emudhra-2026.json`, covering
the affected intermediate keys and the three root SPKIs for AAA Certificate
Services, emSign Root TLS CA-G3 and emSign ECC Root CA-G3; it does not remove certificates.
A failed CA check leaves production tags untouched. Never deploy quarantine
tags. Other application-specific or embedded trust stores still need
independent verification; a newer tag or successful uncached build alone does
not establish eMudhra incident remediation.

For deployment, first merge the workflow and runtime fixes to the default
branch and let all required production builds and CA checks finish. Then pin
the production digest reported in that run in the Hardcover CronJob. A passed feature-branch
candidate is not a production promotion; do not pin its digest as a shortcut.
Preserve those production manifests in the registry while deployments or
rollback revisions reference them.

Hardcover uses Go 1.27.3 and requires Debian Bookworm
`ca-certificates` at least `20250419~deb12u2` and replace the distroless
filesystem's entire certificate and hashed-trust directories, not just its
bundle. Cooklang and Cook Desktop have been retired, including their build
workflows. Shared recipes and the archived desktop configuration PVC are retained.

References: [Debian DSA-6572](https://www.debian.org/security/2026/dsa-6572),
[Ubuntu incident guidance](https://ubuntu.com/blog/emudhra-ca-compromise-fixes-available),
and [Mozilla's affected-key evidence](https://bugzilla.mozilla.org/attachment.cgi?id=9664786).

## Architecture

Hardcover Sync operates as a scheduled CronJob that:
1. Queries Hardcover GraphQL API for user's "Want-To-Read" list (status_id = 1)
2. Resolves book metadata through rreading-glasses for each work ID
3. Adds resolved books to Bookshelf via its API
4. Tracks synced books in a SQLite database to prevent duplicates
5. Runs periodically (default: every hour)

## Flow Diagram

```
┌──────────────┐
│  Hardcover   │
│  GraphQL API │
│              │
│ Want-To-Read │
│     List     │
└──────┬───────┘
       │
       │ 1. Fetch work IDs
       │
       ▼
┌──────────────┐      ┌──────────────┐
│   Hardcover  │─────▶│   rreading-  │
│     Sync     │      │    glasses   │
│              │◀─────│              │
│  (periodic)  │ 2.   │   (8080)     │
└──────┬───────┘ Resolve              
       │         metadata              
       │                               
       │ 3. Add books                 
       ▼                               
┌──────────────┐      ┌──────────────┐
│  Bookshelf   │      │   SQLite DB  │
│   API        │      │   (tracking) │
│  (8787)      │      │              │
└──────────────┘      └──────────────┘
```

## Storage

- **Data PVC**: 100Mi Longhorn volume for SQLite tracking database

## Configuration

### Environment Variables (via ConfigMap)

- `BOOKSHELF_URL`: Bookshelf API URL (default: `http://bookshelf:8787`)
- `RREADING_GLASSES_URL`: rreading-glasses URL (default: `http://rreading-glasses:8080`)
- `SYNC_INTERVAL`: Set to `0` for CronJob mode (run once and exit)
- `DATABASE_PATH`: SQLite database path (default: `/data/hardcover-sync.db`)

### Secret Configuration (via SealedSecret)

Required secrets:
- `HARDCOVER_API_KEY`: API key for Hardcover.app GraphQL API
- `BOOKSHELF_API_KEY`: API key for Bookshelf

## Sealing Secrets

To create and seal the credentials:

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

### Getting API Keys

**Hardcover API Key:**
1. Sign up at https://hardcover.app
2. Go to Settings → API
3. Generate an API key
4. Save for sealed secret creation

**Bookshelf API Key:**
1. Access Bookshelf at https://bookshelf.homelab
2. Go to Settings → General
3. Find or generate API key
4. Save for sealed secret creation

## Security

### Pod Security
- Runs as non-root user (UID 65532, GID 65532)
- FSGroup set to 65532 for volume permissions
- Read-only root filesystem
- All capabilities dropped
- No privilege escalation

### Network Policy
- **No Ingress**: This service doesn't accept incoming connections
- **Egress**: 
  - DNS (port 53)
  - rreading-glasses (port 8080) within media namespace
  - Bookshelf (port 8787) within media namespace
  - HTTPS (port 443) for Hardcover API access

## Resource Limits

- **Requests**: 25m CPU, 32Mi memory
- **Limits**: 100m CPU, 64Mi memory

Hardcover Sync is designed to be extremely lightweight and efficient.

## How It Works

### Initial Sync
1. On startup, fetches current "Want-To-Read" list from Hardcover
2. Checks each book against the SQLite tracking database
3. For new books:
   - Resolves metadata via rreading-glasses
   - Adds to Bookshelf with monitoring enabled
   - Marks as synced in database

### Ongoing Monitoring
1. **Scheduled Runs**: CronJob executes every 2 hours
2. **Duplicate Prevention**: SQLite database tracks synced Hardcover book IDs
3. **Graceful Error Handling**: Continues processing remaining books on errors
4. **Zero Resources When Idle**: No pods running between scheduled executions

### Hardcover GraphQL Query

The service uses the following GraphQL query:

```graphql
query GetWantToRead {
  me {
    user_books(where: {status_id: {_eq: 1}}) {
      book {
        id
        title
        contributions {
          author {
            name
          }
        }
      }
    }
  }
}
```

Status ID 1 represents "Want to Read" in Hardcover.

### Metadata Resolution

For each Hardcover work ID, the service calls:
```
GET http://rreading-glasses:8080/works/<hardcover-id>
```

This returns enriched metadata including:
- Title
- Authors
- ISBN
- Description
- Cover URL

### Bookshelf Integration

Books are added to Bookshelf via:
```
POST http://bookshelf:8787/api/v1/book
```

With payload:
```json
{
  "title": "Book Title",
  "author": "Author Name",
  "isbn": "1234567890",
  "monitored": true,
  "addOptions": {
    "searchForNewBook": true
  }
}
```

The service first checks if the book already exists using:
```
GET http://bookshelf:8787/api/v1/book/lookup?term=<isbn-or-title>
```

## Building the Docker Image

The Go application is located in `src/` directory:

```bash
cd apps/hardcover-sync/src
docker build -t ghcr.io/blazepower/hardcover-sync:v1.0.0 .
docker push ghcr.io/blazepower/hardcover-sync:v1.0.0
```

### Multi-stage Build
The Dockerfile uses a multi-stage build:
1. **Builder stage**: Compiles Go application with static linking
2. **Final stage**: Minimal distroless image with CA certificates

## Troubleshooting

### Check CronJob status
```bash
kubectl get cronjob -n media hardcover-sync
kubectl get jobs -n media -l app.kubernetes.io/name=hardcover-sync
```

### View logs from recent job
```bash
kubectl logs -n media -l app.kubernetes.io/name=hardcover-sync --tail=100
```

### Manually trigger a sync
```bash
kubectl create job --from=cronjob/hardcover-sync -n media hardcover-sync-manual
```

### Check Hardcover API connectivity
```bash
# Check logs for Hardcover API responses
kubectl logs -n media -l app.kubernetes.io/name=hardcover-sync | grep -i hardcover
```

### Check rreading-glasses connectivity
```bash
# Check logs for rreading-glasses interactions
kubectl logs -n media -l app.kubernetes.io/name=hardcover-sync | grep -i "rreading-glasses\|resolving metadata"
```

### Check Bookshelf connectivity
```bash
# Check logs for Bookshelf API interactions
kubectl logs -n media -l app.kubernetes.io/name=hardcover-sync | grep -i "bookshelf\|added book"
```

### Inspect pod environment (ConfigMap values)
```bash
kubectl get pod -n media -l app.kubernetes.io/name=hardcover-sync -o jsonpath='{.items[0].spec.containers[0].env[*].valueFrom.configMapKeyRef.name}' | tr ' ' '\n' | sort -u
kubectl get configmap -n media hardcover-sync-config -o yaml
```

### Common Issues

#### Books not syncing
1. Verify Hardcover API key is valid
2. Check "Want-To-Read" list has books (status_id = 1)
3. Ensure rreading-glasses is running and accessible
4. Verify Bookshelf API key is correct
5. Check database for sync history

#### API errors
1. **Hardcover API**: Verify API key, check rate limits
2. **rreading-glasses**: Ensure service is running, check metadata availability
3. **Bookshelf**: Verify API key, check service status

#### Network connectivity issues
1. Check NetworkPolicy allows egress to required services
2. Verify DNS resolution is working
3. Ensure services are running in correct namespace
4. Check pod security context doesn't block network access

#### Permission errors
1. Verify `/data` directory is writable
2. Check pod runs as UID 65532
3. Ensure PVC is properly mounted
4. Verify fsGroup is set to 65532

## Integration

### Part of Book Management Stack

Hardcover Sync is part of the complete book management ecosystem:

1. **Manual Discovery**: Use Hardcover.app to discover and mark books as "Want-To-Read"
2. **Automatic Sync**: Hardcover Sync picks up the list and adds books to Bookshelf
3. **Acquisition**: Bookshelf automatically searches for and downloads the books
4. **Metadata Enhancement**: rreading-glasses provides enriched metadata
5. **Organization**: Books are organized in Calibre library format
6. **Reading**: Access via Calibre-Web or Kindle Sender

### Dependencies

- **rreading-glasses**: Must be running for metadata resolution
- **Bookshelf**: Must be accessible for adding books
- **Hardcover.app**: External service for "Want-To-Read" list

## Development

The source code is in `apps/hardcover-sync/src/`:
- `main.go`: Main application logic
- `go.mod`: Go module dependencies
- `Dockerfile`: Container build instructions

Dependencies:
- `github.com/mattn/go-sqlite3`: SQLite database driver

## Notes

- SQLite database persists across job runs via PVC
- Books are only synced once (tracked by Hardcover book ID)
- The CronJob runs every 2 hours (configurable via schedule)
- Zero resources consumed between scheduled runs
- Rate limiting: 1 second delay between processing each book
- Errors are logged but don't stop the entire sync process
- Books are added to Bookshelf with `monitored: true` for automatic downloading
- Job history: keeps last 3 successful and 3 failed jobs

## Workflow Integration

This service enables a seamless workflow:

1. **Discovery on Hardcover**: Browse and add books to "Want-To-Read" on Hardcover.app
2. **Automatic Sync**: Hardcover Sync periodically picks up new additions
3. **Metadata Resolution**: rreading-glasses enhances book information
4. **Bookshelf Addition**: Books are automatically added to Bookshelf
5. **Acquisition**: Bookshelf handles downloading and organizing
6. **Delivery**: Kindle Sender automatically delivers to your device

This eliminates manual book management while maintaining control through Hardcover's curation.

## Security Considerations

- API keys are stored as sealed secrets (encrypted at rest)
- No ingress connections accepted (background service only)
- Network policy restricts egress to required services only
- Runs with minimal privileges (non-root, no capabilities)
- Read-only root filesystem prevents tampering
- SQLite database is the only persistent state

## Monitoring

### Logs
Monitor sync operations via logs:
```bash
kubectl logs -n media -l app.kubernetes.io/name=hardcover-sync -f
```

Key log messages:
- Sync start/completion with counts
- Individual book sync success/failure
- API errors and connectivity issues
- Database operations

### Metrics
Pod metrics available via Prometheus:
- CPU and memory usage
- Network traffic
- Pod restarts

### Health Indicators
- Regular sync completion messages
- Low error count in logs
- Stable resource usage
- No pod restarts
