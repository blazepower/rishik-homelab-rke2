# Phantom

> **Internal Reference:** This is an obfuscated application name for internal use.

## Purpose
Automated media management with support for:
- Indexer integration via Prowlarr
- Download client management (SABnzbd, qBittorrent)

## Access
- **Local:** https://phantom.homelab
- **Tailscale:** https://phantom (via Tailscale network)

## Integration Points
- **Prowlarr:** `http://prowlarr.media.svc.cluster.local:9696`
- **SABnzbd:** `http://sabnzbd.media.svc.cluster.local:8080`

## Configuration Notes
- Media stored at `/media` (hostPath mount)
