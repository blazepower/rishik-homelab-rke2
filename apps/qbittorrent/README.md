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

## Security - Executable Blocking

qBittorrent is configured to skip downloading dangerous executable files. This protects against fake releases containing malware.

### Excluded File Names

Files matching these patterns are automatically marked "do not download" - the torrent completes without them.

| Setting | Value |
|---------|-------|
| `excluded_file_names_enabled` | `true` |
| `excluded_file_names` | 110+ wildcard patterns |

### Blocked Extension Categories

| Category | Extensions |
|----------|------------|
| Windows Executables | *.exe, *.com, *.scr, *.pif, *.msi, *.msix, *.msp, *.mst, *.msu, *.dll, *.ocx, *.drv, *.sys, *.cpl, *.bin |
| Scripts - Windows | *.bat, *.cmd, *.vbs, *.vbe, *.vb, *.js, *.jse, *.ws, *.wsf, *.wsc, *.wsh, *.ps1, *.ps1xml, *.ps2, *.ps2xml, *.psc1, *.psc2, *.psm1, *.msh, *.msh1, *.msh2, *.mshxml |
| Scripts - Cross-platform | *.pl, *.sh, *.csh, *.ksh |
| Shortcuts/Links | *.lnk, *.url, *.scf, *.appref-ms, *.website, *.search-ms |
| HTML Applications | *.hta, *.htc, *.mht, *.mhtml, *.xbap |
| Java/JVM | *.jar, *.class, *.jnlp |
| Registry/Config | *.reg, *.inf, *.ins, *.isp, *.job |
| Office Macro-enabled | *.docm, *.dotm, *.xlsm, *.xltm, *.xlam, *.pptm, *.potm, *.ppam, *.ppsm, *.sldm, *.vsdm, *.vstm, *.vssm |
| Access Database | *.ade, *.adp, *.mda, *.mdb, *.mde, *.mdt, *.mdw, *.mdz, *.accda, *.accdb, *.accde, *.accdr |
| Disk Images | *.iso, *.img, *.vhd, *.vhdx |
| Archives (select) | *.cab, *.arj, *.lha, *.lzh, *.ace |
| Windows System | *.gadget, *.diagcab, *.diagcfg, *.diagpkg, *.appinstaller, *.application, *.appx, *.appxbundle, *.settingcontent-ms |
| Legacy/Other | *.chm, *.hlp, *.pcd, *.sct, *.shb, *.grp, *.bas, *.fxp, *.prg, *.crt, *.cer, *.der |

### Sources

Extension list based on:
- [dobin/badfiles](https://github.com/dobin/badfiles) - Curated dangerous file extensions database
- [Microsoft Outlook blocked extensions](https://support.microsoft.com/en-us/office/blocked-file-types-in-outlook)
- [Cleanuparr qBittorrent guide](https://cleanuparr.github.io/Cleanuparr/docs/setup-scenarios/qbit-built-in/)

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
