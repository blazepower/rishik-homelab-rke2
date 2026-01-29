# qBittorrent with ProtonVPN

qBittorrent torrent client running behind ProtonVPN WireGuard VPN via gluetun sidecar.

## Architecture

- **gluetun**: VPN container providing WireGuard tunnel to ProtonVPN
- **qBittorrent**: Torrent client sharing gluetun's network namespace
- All qBittorrent traffic is forced through VPN (kill switch enabled)
- Port forwarding enabled for better speeds and seeding

## Access

- Local: https://qbittorrent.homelab
- Tailscale: https://qbittorrent (via Tailscale network)

## Default Credentials

On first run, qBittorrent generates a random password. Check the logs:

```bash
kubectl logs -n media -l app.kubernetes.io/name=qbittorrent -c app | grep password
```

Default username: `admin`

## Verify VPN Connection

Check that qBittorrent is using VPN IP:

```bash
# Check gluetun public IP
kubectl exec -n media deploy/qbittorrent -c gluetun -- wget -qO- ifconfig.me

# Check port forwarding status
kubectl logs -n media -l app.kubernetes.io/name=qbittorrent -c gluetun | grep -i "port forward"
```

## Arr Stack Integration

### Sonarr/Radarr Configuration

Add qBittorrent as a download client:

| Setting | Value |
|---------|-------|
| Host | `qbittorrent.media.svc.cluster.local` |
| Port | `8080` |
| Username | `admin` |
| Password | (from logs above) |
| Category | `tv` (Sonarr) / `movies` (Radarr) |

### Recommended qBittorrent Settings

In qBittorrent WebUI → Options → Downloads:

- **Default Save Path**: `/media/torrents/complete`
- **Keep incomplete in**: `/media/torrents/incomplete`

Category paths (auto-created):
- Category `tv`: `/media/torrents/complete/tv`
- Category `movies`: `/media/torrents/complete/movies`

This enables hardlinks when Sonarr/Radarr import files to `/media/tv` or `/media/movies`.

## Troubleshooting

### VPN not connecting

```bash
kubectl logs -n media -l app.kubernetes.io/name=qbittorrent -c gluetun
```

### qBittorrent not accessible

Ensure gluetun is healthy first - qBittorrent depends on it:

```bash
kubectl get pods -n media -l app.kubernetes.io/name=qbittorrent
kubectl describe pod -n media -l app.kubernetes.io/name=qbittorrent
```

### Port forwarding not working

ProtonVPN port forwarding requires:
1. NAT-PMP enabled when generating WireGuard config
2. P2P-enabled server selected
3. ProtonVPN Plus or higher plan
