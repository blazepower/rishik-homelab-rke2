# Calibre Converter

Calibre Converter is a custom Go microservice that automatically watches for eBooks in various formats and converts them to EPUB using Calibre's `ebook-convert` CLI tool. After successful conversion, the original file is deleted.

## Overview

- **Image**: `ghcr.io/blazepower/calibre-converter:v1.0.0`
- **Namespace**: media
- **Node**: rishik-worker1
- **Watch Path**: `/media/books`

## Architecture

Calibre Converter is a Go application that:
1. Watches `/media/books` directory for new eBook files
2. Performs periodic scans every 5 minutes (configurable)
3. Tracks converted files in a SQLite database to prevent reprocessing
4. Converts supported formats to EPUB using Calibre's `ebook-convert`
5. Deletes original files after successful conversion

## Features

### Supported Input Formats
- `.pdf` - PDF documents
- `.mobi` - MOBI format (Kindle)
- `.azw3` - AZW3 format (Kindle)
- `.azw` - AZW format (Kindle)
- `.djvu` - DjVu format
- `.docx` - Microsoft Word documents
- `.rtf` - Rich Text Format
- `.txt` - Plain text files
- `.html` / `.htm` - HTML files
- `.cbz` / `.cbr` - Comic book archives

### Output Format
- **Always EPUB** - All input formats are converted to `.epub`

### Conversion Safety
- **Atomicity**: Writes to temp file then moves into place
- **Idempotency**: Skips if EPUB with same base name already exists
- **Verification**: Only deletes original after:
  - `ebook-convert` returns success
  - Output EPUB exists
  - Output EPUB is > 10KB (configurable)
- **Stability Check**: Waits for file size to stabilize before converting (prevents converting files still being written)

### File Watching
- Real-time file system monitoring using fsnotify
- Recursive directory watching
- Periodic scanning as backup (default: 300 seconds / 5 minutes)
- Duplicate detection via SQLite database

### Concurrency
- Configurable worker pool (default: 2 concurrent conversions)
- CPU-intensive operations are properly limited

## Storage

- **Data PVC**: 100Mi Longhorn volume for SQLite state database
- **Media Mount**: hostPath mount of `/media/rishik/Expansion` to `/media` (READ-WRITE)
  - Books directory: `/media/books`
- **Temp Mount**: emptyDir at `/tmp` for read-only root filesystem support

## Configuration

### Environment Variables (via ConfigMap)

| Variable | Default | Description |
|----------|---------|-------------|
| `WATCH_PATH` | `/media/books` | Directory to watch for new books |
| `SCAN_INTERVAL` | `300` | Seconds between periodic scans |
| `MAX_CONCURRENT` | `2` | Maximum concurrent conversions |
| `DATABASE_PATH` | `/data/calibre-converter.db` | SQLite database location |
| `INPUT_EXTENSIONS` | `.pdf,.mobi,...` | Comma-separated list of supported input extensions |
| `OUTPUT_FORMAT` | `epub` | Output format (always epub) |
| `MIN_OUTPUT_SIZE` | `10240` | Minimum output file size in bytes (10KB) |
| `STABILITY_WAIT` | `5` | Seconds to wait for file size to stabilize |

## Security

### Pod Security
- Runs as non-root user (UID 1000, GID 1000)
- FSGroup set to 1000 for volume permissions
- Read-only root filesystem
- All capabilities dropped
- No privilege escalation
- No hostNetwork

### Network Policy
- **No Ingress**: This service doesn't accept incoming connections
- **Egress**: DNS only (port 53) - conversion is entirely local
  - No SMTP, HTTP, or other outbound traffic allowed

## Resource Limits

- **Requests**: 100m CPU, 256Mi memory
- **Limits**: 2000m CPU, 2Gi memory

Conversions are CPU-intensive operations, especially for large PDFs or complex documents.

## How It Works

### Initial Scan
1. On startup, performs a full scan of the watch directory
2. Checks each supported file against the SQLite database
3. Skips files that have already been converted
4. Skips files where EPUB output already exists
5. Queues remaining files for conversion

### Ongoing Monitoring
1. **File Watcher**: Uses fsnotify to detect new files immediately
2. **Stability Check**: Waits for file size to stabilize (file write complete)
3. **Periodic Scan**: Runs every 5 minutes as backup
4. **Duplicate Prevention**: SQLite database tracks converted files

### Conversion Process
1. Check if file is already converted (database)
2. Check if EPUB with same base name exists (idempotency)
3. Wait for file to finish writing (stability check)
4. Convert using `ebook-convert input.pdf output.epub.tmp`
5. Verify temp output exists and is > 10KB
6. Move temp file to final location (atomic rename)
7. Record in database with conversion time
8. Delete original file

## Building the Docker Image

The Go application is located in `src/` directory:

```bash
cd apps/calibre-converter/src
docker build -t ghcr.io/blazepower/calibre-converter:v1.0.0 .
docker push ghcr.io/blazepower/calibre-converter:v1.0.0
```

### Multi-stage Build
The Dockerfile uses a multi-stage build:
1. **Builder stage**: Compiles Go application with static linking
2. **Final stage**: Debian Bookworm with Calibre package

### Container Contents
- Go binary: `/app/calibre-converter`
- Calibre tools: `/usr/bin/ebook-convert` (and other Calibre CLI tools)
- Non-root user: UID 1000

## Troubleshooting

### Check pod status
```bash
kubectl get pods -n media -l app.kubernetes.io/name=calibre-converter
```

### View logs
```bash
kubectl logs -n media -l app.kubernetes.io/name=calibre-converter -f
```

### Verify configuration
```bash
kubectl exec -n media -it <calibre-converter-pod> -- env | grep -E '(WATCH|DATABASE|INPUT|OUTPUT)'
```

### Check database
```bash
kubectl exec -n media -it <calibre-converter-pod> -- ls -la /data/
```

### Verify media mount
```bash
kubectl exec -n media -it <calibre-converter-pod> -- ls -la /media/books/
```

### Test ebook-convert
```bash
kubectl exec -n media -it <calibre-converter-pod> -- ebook-convert --version
```

### Check conversion history
```bash
# The database is in /data/calibre-converter.db
kubectl exec -n media -it <calibre-converter-pod> -- sh
# Then manually inspect the SQLite database if needed
```

### Common Issues

#### Files not being converted
1. Check file format is supported (see Input Formats above)
2. Verify file is not still being written (check logs for stability wait)
3. Check if EPUB with same name already exists
4. Check if file was already converted (database records)
5. Verify watch path contains files: `/media/books/`

#### Conversion errors
1. Check logs for `ebook-convert` error output
2. Verify file is not corrupted
3. Some formats may not convert cleanly (especially complex PDFs)
4. Check temp directory is writable (`/tmp`)

#### Permission errors
1. Verify media mount is accessible
2. Check `/data` directory is writable
3. Verify pod runs as UID 1000
4. Ensure files have correct permissions (readable/writable by UID 1000)

#### Original files not deleted
1. Check logs for deletion errors
2. Verify output EPUB was created successfully
3. Verify output EPUB meets minimum size requirement
4. Check file permissions allow deletion

## Notes

- Media mount is READ-WRITE (required for output EPUBs and deleting originals)
- SQLite database persists across pod restarts
- Files are converted automatically - no manual intervention needed
- Original files are deleted ONLY after successful conversion
- EPUB files in the watch directory are ignored (output format)
- The service is designed to run continuously
- Initial scan may take time depending on library size and number of files to convert

## Integration

### With Bookshelf
- Bookshelf downloads and organizes books to `/media/books/`
- Calibre Converter automatically converts new arrivals to EPUB
- Kindle Sender can then send the EPUBs to Kindle devices

### With Calibre-Web
- Both services can coexist
- Calibre-Web provides a web interface for the library
- Calibre Converter ensures all books are in EPUB format

### With Kindle Sender
- Kindle Sender watches the same directory
- After Calibre Converter creates EPUBs, Kindle Sender can email them
- Both track processed files independently via their own databases

## Development

The source code is in `apps/calibre-converter/src/`:
- `main.go`: Main application logic
- `go.mod`: Go module dependencies
- `go.sum`: Dependency checksums
- `Dockerfile`: Container build instructions

Dependencies:
- `github.com/fsnotify/fsnotify`: File system monitoring
- `github.com/mattn/go-sqlite3`: SQLite database driver

## Workflow Example

1. User downloads `new-book.pdf` to `/media/books/`
2. Calibre Converter detects the new file via fsnotify
3. Waits 5 seconds for file write to complete
4. Runs `ebook-convert new-book.pdf new-book.epub.tmp`
5. Verifies `new-book.epub.tmp` exists and is > 10KB
6. Renames to `new-book.epub`
7. Records conversion in SQLite database
8. Deletes `new-book.pdf`
9. Kindle Sender (if configured) detects `new-book.epub` and emails to Kindle
