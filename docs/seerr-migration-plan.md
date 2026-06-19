# Seerr migration evaluation and plan

## Recommendation

Seerr is viable enough to plan a migration, but make the first production change a reversible image flip only after the current Overseerr bump has settled. Keep SQLite initially; PostgreSQL is optional and can be evaluated separately after Seerr is running.

## Research summary

- Seerr is the merged/rebooted Overseerr + Jellyseerr project. The release announcement says one shared codebase keeps existing Overseerr functionality while adding Jellyseerr features, including Jellyfin and Emby support.
- Latest stable release researched: `v3.3.0`, published 2026-06-02. Recent stable releases: `v3.0.0`/`v3.0.1` on 2026-02-14, `v3.1.0` on 2026-02-27, `v3.1.1` on 2026-04-13, `v3.2.0` on 2026-04-15, `v3.3.0` on 2026-06-02.
- Docker Hub `seerr/seerr` has stable tags: `latest`, `v3`, `v3.3`, `v3.3.0`; it also has `develop` and many SHA/preview tags. `latest` currently points to the 2026-06-02 stable release, while `develop` is rolling and should be avoided for production.
- Official docs also show `ghcr.io/seerr-team/seerr` as the primary image. Docker Hub is documented as an alternative image source.
- Database support is SQLite by default. PostgreSQL is supported but optional (`DB_TYPE=sqlite` is the default; `DB_TYPE=postgres` is opt-in).

## Current cluster/repo state

- Namespace: `media`
- Current HelmRelease: `apps/overseerr/helmrelease.yaml`
- Current image after the Overseerr bump PR: `sctx/overseerr:1.35.0`
- PVC: `overseerr-config`
- Mount path: `/app/config`
- Existing config files in pod: `/app/config/settings.json`, `/app/config/db/db.sqlite3`, plus SQLite WAL/SHM files and logs
- Current env vars: `PUID`, `PGID`, `TZ` from `overseerr-config`

## Feature delta vs current Overseerr

For this Plex + Sonarr/Radarr setup, Seerr should preserve the existing Plex workflow and adds:

- Optional Jellyfin/Emby support, though only one media-server integration can be used at a time.
- Optional PostgreSQL support in addition to SQLite.
- Movie/series/tag blocklists.
- Override rules based on users, tags, and other request conditions.
- Optional TheTVDB metadata for series to better align with Sonarr.
- DNS caching to reduce DNS load.
- ntfy.sh notifications.
- Disable special seasons.
- Newer security posture: rootless container, signed containers/charts, SBOMs.

## Migration compatibility and risks

The migration guide explicitly covers both Overseerr and Jellyseerr. It says no manual migration is required: Seerr automatically migrates an existing Overseerr/Jellyseerr instance on first startup, with an extra configuration migration for Overseerr users.

Main risks for this cluster:

1. **First-start DB/config migration is one-way in practice.** Back up the PVC before changing the image.
2. **Container user changes.** Seerr runs as UID 1000 (`node` user). The existing PVC may need ownership/permissions updated so UID 1000 can read and write `/app/config`.
3. **Kubernetes security context changes.** The Seerr guide expects rootless-compatible `securityContext`/`podSecurityContext` settings such as `fsGroupChangePolicy: OnRootMismatch`, `allowPrivilegeEscalation: false`, and `readOnlyRootFilesystem: false`.
4. **Image source choice.** Docs prefer `ghcr.io/seerr-team/seerr`, while Docker Hub `seerr/seerr` is also documented and has stable tags. Pin a stable version instead of using `develop`.
5. **Service naming.** Keeping the HelmRelease/service/PVC named `overseerr` reduces blast radius, but labels/UI references may remain old until a follow-up rename.

## Proposed low-risk production change

Do not combine this with the Overseerr bump. After this planning PR merges and the cluster is healthy on `sctx/overseerr:1.35.0`, create a separate implementation PR that changes only the deployment configuration.

Suggested implementation diff in `apps/overseerr/helmrelease.yaml`:

```yaml
values:
  controllers:
    overseerr:
      pod:
        securityContext:
          fsGroup: 1000
          fsGroupChangePolicy: OnRootMismatch
      containers:
        app:
          image:
            repository: seerr/seerr
            tag: "v3.3.0"
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: false
          env:
            - name: TZ
              valueFrom:
                configMapKeyRef:
                  name: overseerr-config
                  key: TZ
```

Notes:

- Consider `ghcr.io/seerr-team/seerr:v3.3.0` if GHCR is preferred for signed artifact verification.
- Remove `PUID` and `PGID` unless testing shows Seerr still consumes them; the docs describe UID 1000 ownership instead.
- Keep the existing `overseerr-config` PVC mounted at `/app/config` for the automatic migration.
- Keep SQLite initially. Do not set `DB_TYPE=postgres` for the first migration.

## Pre-flight steps

1. Confirm current health after the Overseerr bump:
   ```powershell
   kubectl rollout status -n media deploy/overseerr
   kubectl logs -n media deploy/overseerr --tail=100
   ```
2. Stop writes by scaling down before backup:
   ```powershell
   kubectl scale -n media deploy/overseerr --replicas=0
   ```
3. Back up the Longhorn/PVC data for `overseerr-config`, including `/app/config/db/db.sqlite3`, `/app/config/db/db.sqlite3-wal`, `/app/config/db/db.sqlite3-shm`, and `/app/config/settings.json`.
4. Ensure PVC permissions allow UID/GID 1000 to write `/app/config`.
5. Apply the implementation PR and reconcile Flux.
6. Watch first-start migration logs:
   ```powershell
   kubectl logs -n media deploy/overseerr -f
   kubectl exec -n media deploy/overseerr -- ls -la /app/config /app/config/db
   ```
7. Validate the UI, Plex login/import, Sonarr/Radarr settings, existing requests, and request approvals.

## Rollback plan

If Seerr fails before completing migration:

1. Scale down Seerr.
2. Revert the image/security-context commit back to `sctx/overseerr:1.35.0`.
3. Restore the `overseerr-config` PVC from the pre-flight backup.
4. Reconcile Flux and verify Overseerr starts.

If Seerr starts and mutates data but behavior is unacceptable, still restore the PVC backup before reverting. Do not point Overseerr at the post-Seerr migrated SQLite DB unless it has been explicitly validated.

## Follow-up: PostgreSQL

PostgreSQL is not required for migration. After Seerr is stable on SQLite, evaluate PostgreSQL as a separate project:

1. Deploy/choose PostgreSQL.
2. Run Seerr once with PostgreSQL to create tables.
3. Stop Seerr.
4. Use the documented `pgloader` flow to copy `/app/config/db/db.sqlite3` into PostgreSQL.
5. Start Seerr with `DB_TYPE=postgres` and the required `DB_HOST`, `DB_USER`, `DB_PASS`, and optional `DB_NAME`/`DB_POOL_SIZE` settings.
