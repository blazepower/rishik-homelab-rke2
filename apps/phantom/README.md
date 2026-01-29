# Phantom

> **Internal Reference:** This is [Whisparr](https://whisparr.com/) v3 - a media management tool for adult content.

## Purpose
Automated media management with support for:
- Indexer integration via Prowlarr
- Download client management (SABnzbd, qBittorrent)
- Stash integration for metadata enrichment

## Access
- **Local:** https://phantom.homelab
- **Tailscale:** https://phantom (via Tailscale network)

## Integration Points
- **Prowlarr:** `http://prowlarr.media.svc.cluster.local:9696`
- **SABnzbd:** `http://sabnzbd.media.svc.cluster.local:8080`
- **Stash:** `http://stash.siloarr.svc.cluster.local:9999`

## Configuration Notes
- Uses Whisparr v3 (hotio image) for Stash support
- Media stored at `/media` (hostPath mount)
