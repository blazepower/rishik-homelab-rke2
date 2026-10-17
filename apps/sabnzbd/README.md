# SABnzbd

SABnzbd is a free and open-source binary newsreader. It simplifies the process of downloading from Usenet by automating the downloading, verification, repairing, and extraction of files.

## Overview

This deployment uses the [bjw-s app-template Helm chart](https://github.com/bjw-s/helm-charts/tree/main/charts/other/app-template) with the linuxserver.io SABnzbd container image.

## SABnzbd 5.1

The 5.1 container automatically benefits from retrying only missing articles, container-aware Article Cache limits, and improved shutdown handling. Future post-processing scripts must parse `SAB_FILES` as a JSON list rather than treating it as a plain string.

## Configuration

| Setting | Value |
|---------|-------|
| **Namespace** | `media` |
| **Node** | `rishik-worker1` (pinned via nodeSelector) |
| **Storage Class** | `longhorn` |
| **Config PVC Size** | 1Gi |
| **User/Group** | PUID=1000, PGID=1000 |
| **Timezone** | America/Los_Angeles |
| **Media Mount** | `/media/rishik/Expansion` -> `/media` (hostPath) |
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

SABnzbd uses a startup probe to allow approximately ten minutes for the Docker
Mod/package installation and application startup. Kubernetes suppresses liveness
and readiness checks until this version-endpoint check succeeds, preventing a
slow install from being interrupted by liveness restarts. Existing liveness and
readiness timings remain unchanged afterward:

| Probe | Path | Period | Timeout | Failure Threshold |
|-------|------|--------|---------|-------------------|
| Startup | `/sabnzbd/api?mode=version` | 10s | 5s | 60 |
| Liveness | `/sabnzbd/api?mode=version` | 30s | 10s | 3 |
| Readiness | `/sabnzbd/api?mode=version` | 10s | 5s | 3 |

## History Retention

The codec pilot init reconciler manages the following native settings in
`/config/sabnzbd.ini`. Failed records must remain visible for Sonarr and manual
review; this deliberately replaces the previous legacy `1d`/`failed` policy.

| Setting | Value | Description |
|---------|-------|-------------|
| `history_retention` | empty | Do not run the legacy retention converter |
| `history_retention_option` | `all` | Retain history; no automatic archive/delete |
| `new_nzb_on_failure` | `0` | Do not fetch indexer-provided failure alternatives |
| `fail_hopeless_jobs` | `1` | Quickly fail jobs that can't complete |
| `fast_fail` | `1` | Enable fast failure detection |

The last two settings are existing settings, not rewritten by the reconciler.
Monitor history growth during the pilot rather than automatically purging failures.

## TV actual-codec pilot

`codec-guard.yaml` mounts a read-only, executable Python post-processing script
at `/codec-guard/scripts/codec-guard.py`. It inspects actual video metadata, not
release names: **H.264 10-bit / High 10 fails**, while ordinary H.264 8-bit and
HEVC 10-bit pass. The `tv` category alone selects it. Movies, qBittorrent,
existing library files and seeding torrents are outside its scope. In particular,
this cannot repair previously downloaded, mislabeled High 10 Jujutsu episodes.
No transcoder is installed as a service and no file conversion is performed.

### Native settings and dependencies

Before SAB starts, the `codec-config` init container uses the same SAB image and
its bundled `configobj` library to reconcile only:

- `misc.script_dir=/codec-guard/scripts`
- `misc.script_can_fail=1`
- the three retention/alternative settings listed above
- `categories.tv.script=codec-guard.py`

Other settings, credentials, providers, category paths, processing modes and
cleanup rules are preserved. Configuration contents are never logged. The write
is staged on the config PVC with restrictive permissions, parsed/read back,
ownership/mode preserved, and atomically replaced. Parsing errors, existing
script directories/category scripts, changed TV paths, video cleanup rules and
concurrent writes block startup rather than silently replacing integrations.
`Recreate` is intentional: the old SAB process must exit before the init
container edits its INI. This rollout incurs a SAB outage, including startup
package installation time; it does not require a node reboot or media restart.
Removing the init container alone does not undo the persisted native settings.

Preflight on October 16, 2026 captured pod
`sabnzbd-c577845bd-4j2hb` on `rishik-worker1`: ready, zero restarts,
no prior termination state, running since September 28, about 116 MiB memory
and 2m CPU, 200m/512Mi requests and 2 CPU/2Gi limits, no pod events.
Recent log event metadata was inspected without emitting messages/URLs.
The config and media PVC mounts were verified before manifest edits.
The image has Python/configobj but **does not have ffprobe**. Existing TV
script is `Default`, global script is `None`, script directory is empty and
`script_can_fail=0`; therefore no existing script needs replacement.

The official LinuxServer
[universal-package-install mod](https://github.com/linuxserver/docker-mods/tree/universal-package-install)
installs Alpine's `ffmpeg` package (which supplies `ffprobe`) during app startup.
The mod is digest-pinned; the installed Docker Mod loader was checked to support
`tag@sha256` references. Package repositories still select their current ffmpeg
package: **registry and Alpine package-network availability are startup
dependencies**, not a self-contained custom image. Validate `ffprobe -version`
after rollout. Missing or broken ffprobe makes TV validation fail closed.

### Validation and retention policy

- SAB 5.1.3 supplies `SAB_COMPLETE_DIR`, `SAB_CAT` and `SAB_PP_STATUS` as
  documented in its [post-processing script contract](https://sabnzbd.org/wiki/configuration/5.1/scripts/post-processing-scripts).
  `SAB_STATUS` normally says `Running` while an external script executes.
  Any nonzero/missing post-processing status, failed/unknown job state, missing
  directory or path outside a completed TV job fails; it is never reclassified
  as a successful download by this script.
- A nonzero script exit **does not fail SAB by default**. The managed
  `script_can_fail=1` is mandatory. SAB 5.1.3 `postproc.py` was checked: it
  sets `all_ok=False` on a script error only with this flag and subsequently
  uses `purge_data(delete_all_data=all_ok)`, retaining failed job files.
- All main videos, including extras, are inspected; any incompatible stream
  rejects the whole job. Attached cover pictures are ignored. Only explicit
  `Sample`/`Samples` directories or delimited `sample` filenames are skipped
  with a log entry. Sample-only/no-video TV jobs fail for manual review.
  Other sidecars are not probed. No files are renamed, rewritten or deleted.
- Probe failure, timeout, missing/unknown stream metadata and malformed JSON
  all fail closed, separately from an incompatible codec. Per-file timeout is
  at most 30 seconds; overall scanning/probing budget is 300 seconds, 100 main
  videos and 10,000 directory entries. ffprobe analysis, stream count and
  allocations are bounded. Large batches exceeding these limits require review.
  Individual allocations are capped at 64 MiB: the initial 16 MiB limit caused
  ffprobe to misparse a valid Matroska file with large attachments and report
  an extra video stream with unknown metadata. Real-file verification confirmed
  that 64 MiB reads its ordinary H.264 8-bit stream correctly; unknown streams
  still fail closed rather than being ignored.
  Only local-file protocols are permitted; network-backed stream shortcuts
  cannot make ffprobe request remote media.
  Logs contain escaped basenames and fixed reasons, not probe stderr or URLs.
- **Rollout gate:** Sonarr's SAB client must have
  `removeFailedDownloads=false`, otherwise its own failure handling can remove
  the files/history SAB retained. The read-only preflight found this flag
  **true**; the companion Sonarr GitOps policy must change and verify it before
  enabling this guard. Keep automatic failed redownload enabled if desired.
  Sonarr 4.0.16 has `enableCompletedDownloadHandling=true` and
  `autoRedownloadFailed=true`; no separate `enableFailedDownloadHandling` field
  was returned by its API. Do not infer such a setting.

### Offline tests and controlled rollout

From the repository root (CI needs `pyyaml` and `configobj`):

```powershell
python .github\scripts\test-sab-codec-guard.py
kubectl kustomize apps\sabnzbd
```

The tests use synthetic metadata and dummy INI credentials. They cover H.264
8-bit, each High 10 signal, HEVC 10-bit, multiple files/streams, samples,
no-media, malformed/unreadable probes, bounds, category/status gating, atomic
configuration failure and preservation of every unrelated setting. No test
adds jobs, deletes client history, accesses real media or requests downloads.
The current SAB image also passed a read-only reconciliation-precondition check
and the non-TV skip/existing-failure smoke cases using the proposed code in
memory; no scripts were installed or native settings changed during preflight.

Only after parent review, merged GitOps rollout and the Sonarr retention gate:

```powershell
flux reconcile kustomization apps --with-source
kubectl rollout status deployment/sabnzbd -n media --timeout=10m
kubectl logs deployment/sabnzbd -n media -c codec-config
kubectl exec deployment/sabnzbd -n media -- python3 /codec-guard/reconcile.py --check
kubectl exec deployment/sabnzbd -n media -- ffprobe -version
```

Verify HelmRelease readiness, mounts and no restarts; review init output only
(do not dump the INI or unrestricted application logs). `--check` is read-only
and reports managed drift without displaying credentials. Recheck Sonarr's
safe handling/removal fields and SAB's effective live category/script flag via
allowlisted local API reads; stored INI verification alone is not proof that a
running process loaded them.

For a no-download executable smoke check, invoke the script with
`SAB_CAT=movies SAB_PP_STATUS=0 SAB_STATUS=Running`: it must explicitly skip and
exit 0. Invoke it with `SAB_CAT=tv SAB_PP_STATUS=2 SAB_STATUS=Failed`: it must
report existing failure and exit 1 without touching any files or client items.
The offline fixtures plus the verified native `script_can_fail` contract prove
the safe failure path without manufacturing a real SAB history item. On the
first naturally occurring rejection, verify SAB history says Failed, Sonarr
reports failed download/blocklist handling rather than import, and completed
files/history remain. Do not trigger downloads or delete existing records to
test this integration. Full live Sonarr observation remains a rollout check,
not an action performed by these tests.

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
