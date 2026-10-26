# Kaneo Project Management

The official Kaneo Helm chart deploys a single bundled API/web application
(`ghcr.io/usekaneo/kaneo`) and PostgreSQL 16. The chart source and immutable
application image are updated together; obsolete `api.*` and `web.*` values do
not configure the bundled chart.

## Configuration

| Setting | Value |
|---------|-------|
| Namespace | `kaneo` |
| Application service | `kaneo-kaneo:5173` |
| Database service | `kaneo-postgresql:5432` |
| Database storage | Existing `kaneo-postgresql-data`, 8Gi Longhorn, ReadWriteOnce |
| Ingress | `https://kaneo.homelab`, Traefik with TLS |
| Path routing | Both `/api` and `/` use the bundled application service |
| Registration | Enabled |

| Component | CPU request | CPU limit | Memory request | Memory limit |
|-----------|-------------|-----------|----------------|--------------|
| PostgreSQL | 50m | 200m | 128Mi | 256Mi |
| Bundled application | 100m | 500m | 256Mi | 1Gi |

The existing `kaneo-credentials` Secret supplies `AUTH_SECRET` from `jwt-secret`,
`DATABASE_URL` from `database-url`, and the database password from
`postgres-password`. Preserve these references and the original authentication
secret across upgrades. The chart consumes them directly; no generated
credentials or split-image overrides are needed.

The bundled image runs as UID 1001. Its ephemeral runtime volume at
`/var/lib/kaneo` uses `fsGroup: 1001`; application records remain in PostgreSQL.
Readiness, liveness and startup probes use `/api/health`. The startup probe
allows five minutes for database migrations before liveness checks begin.

## Upgrade and recovery

Both Deployments use `Recreate`: old and new application versions must not run
startup migrations concurrently, and PostgreSQL must not have competing writers
on its existing RWO volume. The database image remains pinned to the current
PostgreSQL 16 binary rather than introducing a database-major upgrade.

Before a chart/image upgrade, require an encrypted native database backup,
verified isolated restore, and target-version migration rehearsal. Temporarily
suspend the HelmRelease while reconciling the coordinated chart source and
Helm values, verify both desired revisions, then resume it during the maintenance
window. This prevents the source controller from deploying the new chart with
old values between separate GitOps reconciliations.

Automatic upgrade remediation is disabled because rolling back the application
image does not undo schema or data migrations. On a failed migration, keep
application writers stopped and recover using the verified database backup and
matching application version. Do not rely on an image-only Helm rollback.

## Direct access

```shell
kubectl port-forward -n kaneo svc/kaneo-kaneo 8080:5173
```

The web interface is available at `http://localhost:8080`; the API health endpoint
is `http://localhost:8080/api/health`.

The Traefik ingress, Tailscale ingress and network policy remain managed
separately from the chart.
