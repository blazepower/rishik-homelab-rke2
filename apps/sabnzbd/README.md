# SABnzbd

SABnzbd is a free and open-source binary newsreader. It simplifies the process of downloading from Usenet by automating the downloading, verification, repairing, and extraction of files.

## Overview

This deployment uses the [bjw-s app-template Helm chart](https://github.com/bjw-s/helm-charts/tree/main/charts/other/app-template) with the linuxserver.io SABnzbd container image.

## Configuration

| Setting | Value |
|---------|-------|
| **Namespace** | `media` |
| **Node** | `rishik-worker1` (pinned via nodeSelector) |
| **Storage Class** | `longhorn` |
| **Config PVC Size** | 1Gi |
| **User/Group** | PUID=1000, PGID=1000 |
| **Timezone** | America/Los_Angeles |
| **Media Mount** | `/media/rishik/Expansion` → `/media` (hostPath) |
| **Service Type** | ClusterIP (internal only) |
| **Ingress** | `sabnzbd.homelab` |
| **Port** | 8080 |
| **TLS** | Enabled via cert-manager with `cluster-ca` ClusterIssuer |

## Resource Limits

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| SABnzbd | 200m | 2000m (2 cores) | 512Mi | 2Gi |

**Note**: SABnzbd has higher resource limits than other *arr apps to handle CPU-intensive unpacking and verification tasks.

## Health Checks

SABnzbd is configured with liveness and readiness probes to detect UI hangs and automatically restart:

| Probe | Path | Period | Timeout | Failure Threshold |
|-------|------|--------|---------|-------------------|
| Liveness | `/sabnzbd/api?mode=version` | 30s | 10s | 3 |
| Readiness | `/sabnzbd/api?mode=version` | 10s | 5s | 3 |

## History Retention

Configured in `/config/sabnzbd.ini` to prevent history buildup that can cause UI issues:

| Setting | Value | Description |
|---------|-------|-------------|
| `history_retention` | `1d` | Auto-delete items after 1 day |
| `history_retention_option` | `failed` | Apply only to failed downloads |
| `fail_hopeless_jobs` | `1` | Quickly fail jobs that can't complete |
| `fast_fail` | `1` | Enable fast failure detection |

## Security - Executable Blocking

SABnzbd is configured to block downloads containing dangerous executable files. This protects against fake releases containing malware.

### Unwanted Extensions Settings

| Setting | Value | Description |
|---------|-------|-------------|
| `unwanted_extensions` | 110+ extensions | See full list below |
| `action_on_unwanted_extensions` | `2` (abort) | Immediately abort download |
| `unwanted_extensions_mode` | `1` (strict) | Check during download, not after |

### Blocked Extension Categories

| Category | Extensions |
|----------|------------|
| Windows Executables | exe, com, scr, pif, msi, msix, msp, mst, msu, dll, ocx, drv, sys, cpl, bin |
| Scripts - Windows | bat, cmd, vbs, vbe, vb, js, jse, ws, wsf, wsc, wsh, ps1, ps1xml, ps2, ps2xml, psc1, psc2, psm1, msh, msh1, msh2, mshxml |
| Scripts - Cross-platform | pl, sh, csh, ksh |
| Shortcuts/Links | lnk, url, scf, appref-ms, website, search-ms |
| HTML Applications | hta, htc, mht, mhtml, xbap |
| Java/JVM | jar, class, jnlp |
| Registry/Config | reg, inf, ins, isp, job |
| Office Macro-enabled | docm, dotm, xlsm, xltm, xlam, pptm, potm, ppam, ppsm, sldm, vsdm, vstm, vssm |
| Access Database | ade, adp, mda, mdb, mde, mdt, mdw, mdz, accda, accdb, accde, accdr |
| Disk Images | iso, img, vhd, vhdx |
| Archives (select) | cab, arj, lha, lzh, ace |
| Windows System | gadget, diagcab, diagcfg, diagpkg, appinstaller, application, appx, appxbundle, settingcontent-ms |
| Legacy/Other | chm, hlp, pcd, sct, shb, grp, bas, fxp, prg, crt, cer, der |

### Cleanup List

After extraction, the following junk files are automatically removed:
```
nfo, sfv, srr, info, lnk, url, txt
```

### Sources

Extension list based on:
- [dobin/badfiles](https://github.com/dobin/badfiles) - Curated dangerous file extensions database
- [Microsoft Outlook blocked extensions](https://support.microsoft.com/en-us/office/blocked-file-types-in-outlook)
- Email security vendors (Barracuda, Proofpoint, Trend Micro)

## Access

- **HTTPS**: `https://sabnzbd.homelab` (via Traefik ingress with TLS)

## Port Forwarding (for debugging)

To access SABnzbd directly via port forwarding:
```bash
kubectl port-forward -n media svc/sabnzbd 8080:8080
```
Then access at: `http://localhost:8080`

## Files

- `helmrelease.yaml` - HelmRelease using bjw-s app-template chart
- `pvc.yaml` - 1Gi Longhorn PVC for config storage
- `configmap.yaml` - Environment variables (PUID, PGID, TZ)
- `ingress.yaml` - Traefik ingress for sabnzbd.homelab
- `certificate.yaml` - TLS certificate via cert-manager
- `kustomization.yaml` - Kustomize configuration

## Purpose

SABnzbd serves as the download client for the *arr stack:
- Downloads NZB files from Usenet
- Automatically verifies and repairs downloads using PAR2 files
- Extracts archives (RAR, ZIP, etc.)
- Manages download queue and priorities
- Reports completion status to Sonarr/Radarr

## Directory Structure

- `/media/downloads/complete/tv/` - Completed TV downloads
- `/media/downloads/complete/movies/` - Completed movie downloads
- `/media/downloads/incomplete/` - In-progress downloads

## Migration

Configuration should be restored from backup to maintain:
- Usenet server settings
- Category configurations
- Download paths
- API keys (used by Sonarr/Radarr)

## Environment Variables

Configured via ConfigMap:
- `PUID=1000` - User ID for file permissions
- `PGID=1000` - Group ID for file permissions
- `TZ=America/Los_Angeles` - Timezone for scheduling

## Integration

SABnzbd integrates with:
- **Sonarr**: Receives TV show download requests
- **Radarr**: Receives movie download requests
- Sends completion notifications back to requesting applications
