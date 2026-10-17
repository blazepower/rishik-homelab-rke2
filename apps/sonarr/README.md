# Sonarr

Sonarr is a PVR for Usenet and BitTorrent users. It can monitor multiple RSS feeds for new episodes of your favorite shows and will grab, sort, and rename them.

## Overview

This deployment uses the [bjw-s app-template Helm chart](https://github.com/bjw-s/helm-charts/tree/main/charts/other/app-template) with the linuxserver.io Sonarr container image.

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
| **Ingress** | `sonarr.homelab` |
| **Port** | 8989 |
| **TLS** | Enabled via cert-manager with `cluster-ca` ClusterIssuer |

## Resource Limits

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Sonarr | 100m | 1000m (1 core) | 256Mi | 1Gi |

## Access

- **HTTPS**: `https://sonarr.homelab` (via Traefik ingress with TLS)

## Port Forwarding (for debugging)

To access Sonarr directly via port forwarding:
```bash
kubectl port-forward -n media svc/sonarr 8989:8989
```
Then access at: `http://localhost:8989`

## Files

- `helmrelease.yaml` - HelmRelease using bjw-s app-template chart
- `pvc.yaml` - 1Gi Longhorn PVC for config storage
- `configmap.yaml` - Environment variables (PUID, PGID, TZ)
- `ingress.yaml` - Traefik ingress for sonarr.homelab
- `certificate.yaml` - TLS certificate via cert-manager
- `kustomization.yaml` - Kustomize configuration

## Purpose

Sonarr automates the management of TV shows:
- Monitors for new episodes
- Searches indexers (via Prowlarr) for releases
- Sends download requests to SABnzbd
- Organizes and renames files after download
- Updates library metadata

## Directory Structure

- `/media/downloads/complete/tv/` - Completed TV downloads
- `/media/downloads/incomplete/` - In-progress downloads

## Migration

Configuration should be restored from backup to maintain:
- TV show library
- Quality profiles
- Download client settings
- Indexer connections (via Prowlarr)

## Environment Variables

Configured via ConfigMap:
- `PUID=1000` - User ID for file permissions
- `PGID=1000` - Group ID for file permissions
- `TZ=America/Los_Angeles` - Timezone for scheduling

Configured directly in HelmRelease (Sonarr v4 config overrides using `SONARR__<XML_TAG>` format):
- `SONARR__AUTHENTICATIONMETHOD=Forms` - Use web-based login form instead of browser Basic auth prompt
- `SONARR__AUTHENTICATIONREQUIRED=DisabledForLocalAddresses` - Disable authentication for local/private network addresses (e.g. behind Tailscale or Traefik ingress)

## Integration

### TV codec compatibility

The `apps/media-codec-policy` CronJobs reconcile a shared High 10 H.264
rejection custom format into every Sonarr and Radarr quality profile every
six hours. It preserves existing quality, language, upgrade, and format
preferences, and sets a negative score that cannot be offset by other
positive format scores. Normal H.264, HEVC (including 10-bit), and AV1 are
not rejected by this policy.

Detection depends on release titles advertising Hi10P, High 10, or H.264
with 10-bit depth; mislabeled releases can still slip through. The job does
not start searches, delete media, or change upgrade settings. Existing
incompatible files can still require Plex conversion or manual replacement.
Each job runs alongside its application and reads only that application's
config mount. API keys stay in memory and are never stored in Git.

### Unattended import safeguards

The Sonarr job also reconciles native **Release Profiles** every six hours.
These are hard "Must Not Contain" exclusions (`ignored` in the v4 API), not
custom-format score preferences. They apply to all indexers, including
Prowlarr-managed ones, but only to the following installed series:

| TVDB ID | Managed tag | Excluded release-title style |
|---------|-------------|------------------------------|
| 371310 (Mushoku Tensei) | `gitops-import-371310` | `[sam]` English series title with ambiguous `S2 - 04` numbering (including other episode numbers) |
| 371310 (Mushoku Tensei) | `gitops-import-371310` | `[EMBER]` series title containing `Season 1 Part 02` and a parenthesized/bracketed `Batch` marker |
| 377543 (Jujutsu Kaisen) | `gitops-import-377543` | `[Almighty]` series title with ambiguous `S3 Vol.3` volume numbering (including other volume numbers) |

Matching ignores case and accepts spaces, underscores, dots, and hyphens
between the relevant title/numbering terms. The exclusions target the
demonstrated season/part/volume parsing failures: they do not globally ban
these groups, anime batches, HEVC, or 10-bit video. Straightforward
`S02E04`/`S03E07` releases without those ambiguous pack descriptions and
releases from other groups remain eligible under this policy. A batch
description may still be excluded even if an embedded filename contains
`S01E15`; that did not make the observed outer release title safe to parse.

This deliberately trades some availability for unattended-import safety:
a correctly numbered file inside one of these ambiguous releases will not
make that release eligible for automatic grabbing. Partial packs and
unmatched episodes still need review. There are no forced imports, library
renames, searches, indexer changes, or seeding changes. The only managed
download-client change is failed-download retention described below.

Managed profiles are named `GitOps import safety: Mushoku Tensei` and
`GitOps import safety: Jujutsu Kaisen`. The reconciler locates series by
their stable TVDB IDs, unions the dedicated tag with existing tags, and
preserves other series settings and user-created Release Profiles. It
repairs drift in its own profiles/tags without changing quality preferences
or upgrade flags. Do not reuse these dedicated tags on other series:
the job fails explicitly rather than broadening their scope. An absent
series is explicitly logged and skipped; API lookup/schema errors fail
the job rather than being interpreted as absence. Radarr retains only
the existing High 10 custom-format behavior.

The same Sonarr job appends missing native metadata tokens to standard,
daily, and anime episode naming formats:

```text
[{Quality Full}] [{MediaInfo VideoCodec}] [{MediaInfo VideoBitDepth}bit] [{MediaInfo AudioCodec}] {MediaInfo AudioLanguages} -{Release Group}
```

Existing series/season/episode prefixes, folder formats, naming preferences,
and the rename-enabled setting are retained. Existing equivalent metadata
tokens are not appended again. Before writing, the job validates the
candidate formats using Sonarr's read-only `config/naming/examples`
endpoint, including standard, daily, anime, and multi-episode samples;
invalid samples stop prevention changes. API shapes and token behavior
were checked against Sonarr 4.0.16.2944. Read-back checks verify the saved
managed settings after reconciliation.

This only preserves the provenance available to Sonarr on **future**
imports when renaming is enabled. It does not run `RenameFiles`, restore
metadata stripped from older filenames, verify file contents from titles,
or guarantee that a manual-import custom-format preview of 700 will remain
700 after import instead of dropping to 0. Codec detection still depends
on advertised titles and available media information; mislabeled content
and novel numbering conventions can still require intervention.

After GitOps rollout, inspect the Sonarr policy job logs for import
prevention/naming verification, then check the two tagged Release Profiles
and naming samples in Sonarr. The next six-hour run reapplies managed
settings; make permanent policy adjustments in
`apps/media-codec-policy/script.yaml`, not solely in the UI. Existing queue
items are not retroactively repaired by these acquisition restrictions.

### Retaining SABnzbd codec rejections for review

Before applying Sonarr's native import safeguards, the Sonarr policy job
sets the SABnzbd client's top-level `removeFailedDownloads` to `false`
through the native `downloadclient/{id}` API. This prevents Sonarr's failed
download handling from removing SAB history/files retained after the SAB
codec guard rejects a completed download. This is a prerequisite for
rolling out that guard, not an instruction to import the rejected content.

The reconciler identifies exactly one client by implementation `Sabnzbd`
and contract `SabnzbdSettings`, never by its editable display name. It
preserves `removeCompletedDownloads`, global `autoRedownloadFailed`, all
client fields/credentials, other client preferences, and non-SAB clients
(including qBittorrent). Radarr clients are never read or changed.
Missing/duplicate SAB clients, inconsistent identities, and malformed API
responses fail explicitly without a retention write. Read-back verifies
the retention flag and preservation of the client collection and global
download settings; failures stop the job. Client configurations and
credentials are not logged. The next scheduled run repairs retention drift.

Retention applies to **all failed SAB downloads managed by Sonarr**, not
just codec failures. Failed history and payloads may consume space until
reviewed and deliberately cleaned up. Automatic redownload remains at its
existing setting (currently enabled), so alternative-release retries can
continue. Successful-download removal also remains at its existing
setting. This does not override SAB's own cleanup configuration, provide
indefinite archival guarantees, or undo manual deletion.

Sonarr integrates with:
- **Prowlarr**: For indexer management
- **SABnzbd**: For downloading content
- **Plex**: For media library updates

## Superseded Download Cleanup

The `sonarr-queue-cleanup` CronJob runs every 12 hours and removes completed
downloads that Sonarr has rejected only because an equal or better episode
file already exists. A download must remain in the warning state for at least
24 hours before cleanup.

The cleanup removes the item from Sonarr and its download client, deletes its
downloaded files, and does not blocklist the release. Warnings involving
unmatched episodes, missing files, permissions, or other import errors are
preserved for manual review.
