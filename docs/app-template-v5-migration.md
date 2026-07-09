# app-template v5 per-app migration plan

## Executive summary

This repository has 15 HelmReleases pinned to `bjw-s/app-template` `3.5.1`: bazarr, bookshelf, calibre-web, homebox, kindle-sender, overseerr, phantom, prowlarr, qbittorrent, radarr, rreading-glasses, sabnzbd, sonarr, syncthing, and vault. PR #183 attempted to migrate all of them together, but that is too much blast radius for a major chart upgrade and CI's `kustomize-build` does not render Flux HelmReleases, so a green CI result is not proof that the chart values still render or upgrade safely. The safer approach is one application per PR, with the app-template chart version pinned in each HelmRelease, rendered locally with Helm, applied, observed for at least 24 hours, and only then followed by the next app.

## Upstream v4/v5 changes that matter here

Sources checked:

- `app-template-4.0.0` release notes: app-template upgraded the common library to `4.0.0`.
- `common-4.0.0` release notes: standardized resource naming, Kubernetes minimum `>=1.28`, ServiceAccounts no longer create a static token by default, and the controller label changed from `app.kubernetes.io/component` to `app.kubernetes.io/controller`.
- `app-template-5.0.0` / `5.0.1` release notes: app-template upgraded the common library to v5.
- `common-5.0.0` release notes and v5 values docs: `rawResources` moved from `spec` to `manifest`; `automountServiceAccountToken` now defaults to `false`; an unprivileged ServiceAccount is created by default unless `global.createDefaultServiceAccount=false`; ServiceMonitor/PodMonitor `jobLabel` defaults to `app.kubernetes.io/name`; NetworkPolicy `controller` and `podSelector` are mutually exclusive; the common release notes call out Helm `>=3.18` and Kubernetes `>=1.31` for v5.

Local best-effort rendering with `helm template ... bjw-s/app-template --version 3.5.1` vs `--version 5.0.1` found no schema render failures for these values, but it did confirm cross-cutting manifest changes:

- Every app gets a new `ServiceAccount/<release>` in v5.
- Every pod changes from `serviceAccountName: default` / `automountServiceAccountToken: true` to `serviceAccountName: <release>` / `automountServiceAccountToken: false` unless explicitly overridden.
- Every Deployment selector and Service selector changes from `app.kubernetes.io/component=<controller>` to `app.kubernetes.io/controller=<controller>`. Deployment selectors are immutable, so expect delete/recreate or Helm force behavior during each app migration.
- Services kept their current names in the local render.
- Chart-managed PVC names changed for `homebox` and `syncthing`; this is the largest data-loss risk and must be handled explicitly.

## Per-app analysis

| App | Current pin | Risk | Breaking changes affecting this app | LB/PVC/Ingress impact |
| --- | --- | --- | --- | --- |
| bazarr | 3.5.1 | LOW | Values render on v5. Cross-cutting selector label changes, new default ServiceAccount, and `automountServiceAccountToken=false`. Existing PVCs only (`bazarr-config`, `media-root`). | No LB or Ingress. No chart-managed PVC. Service name stays `bazarr`. |
| bookshelf | 3.5.1 | LOW | Values render on v5. Uses `defaultPodOptions.securityContext`, which still renders. Cross-cutting selector/ServiceAccount/token changes. Existing PVCs only. | No LB or Ingress. No chart-managed PVC. Service name stays `bookshelf`. |
| calibre-web | 3.5.1 | LOW | Values render on v5. Uses `defaultPodOptions.securityContext`, which still renders. Cross-cutting selector/ServiceAccount/token changes. Existing PVCs only. | No LB or Ingress. No chart-managed PVC. Service name stays `calibre-web`. |
| homebox | 3.5.1 | HIGH | Values render on v5, but `persistence.data` is chart-managed (`type` defaults to PVC with `storageClass: longhorn`, `size: 5Gi`). Cross-cutting selector/ServiceAccount/token changes. | PVC render changes from `homebox-data` to `homebox`. Do not apply without preserving/adopting data or retaining the old claim. No LB or Ingress. |
| kindle-sender | 3.5.1 | MEDIUM | Values render on v5. Uses `serviceMonitor`; rendered `jobLabel` changes from `kindle-sender` to `app.kubernetes.io/name` unless pinned. Cross-cutting selector/ServiceAccount/token changes. | No LB or Ingress. Existing PVCs only. Confirm Prometheus target and dashboard labels after migration. |
| overseerr | 3.5.1 | LOW | Values render on v5. Uses `defaultPodOptions.securityContext`, which still renders. Cross-cutting selector/ServiceAccount/token changes. Existing PVC only. | No LB or Ingress. No chart-managed PVC. Service name stays `overseerr`. |
| phantom | 3.5.1 | LOW | Values render on v5. HelmRelease name is `phantom`, controller is `whisparr`; render kept Deployment/Service names stable as `phantom` and selected controller labels correctly. Cross-cutting selector/ServiceAccount/token changes. | No LB or Ingress. Existing PVCs only. Service name stays `phantom`. |
| prowlarr | 3.5.1 | LOW | Values render on v5. Cross-cutting selector/ServiceAccount/token changes. Existing PVC only. | No LB or Ingress. No chart-managed PVC. Service name stays `prowlarr`. |
| qbittorrent | 3.5.1 | HIGH | Values render on v5, including `advancedMounts` and `emptyDir`, but operational risk is high: multi-container pod, VPN sidecar with `NET_ADMIN`, app container depends on gluetun, and a sync sidecar depends on both qBittorrent and VPN port-forward state. Cross-cutting selector/ServiceAccount/token changes. | No LB or Ingress. Existing PVCs only. Service name stays `qbittorrent`. Watch VPN tunnel, qBittorrent web UI, port-forward sync, and MAM update loop. |
| radarr | 3.5.1 | LOW | Values render on v5. Cross-cutting selector/ServiceAccount/token changes. Existing PVCs only. | No LB or Ingress. No chart-managed PVC. Service name stays `radarr`. |
| rreading-glasses | 3.5.1 | LOW | Values render on v5. Uses `defaultPodOptions.securityContext`, which still renders. No persistence. Cross-cutting selector/ServiceAccount/token changes. | No LB, Ingress, or PVC. Service name stays `rreading-glasses`. Best pilot candidate. |
| sabnzbd | 3.5.1 | LOW | Values render on v5. Cross-cutting selector/ServiceAccount/token changes. Existing PVCs only. | No LB or Ingress. No chart-managed PVC. Service name stays `sabnzbd`. |
| sonarr | 3.5.1 | LOW | Values render on v5. Cross-cutting selector/ServiceAccount/token changes. Existing PVCs only. | No LB or Ingress. No chart-managed PVC. Service name stays `sonarr`. |
| syncthing | 3.5.1 | HIGH | Values render on v5, but this app has the most app-specific churn: chart-managed config PVC, ClusterIP service, LoadBalancer sync service, Ingress, custom probes, and pod-level security context. Cross-cutting selector/ServiceAccount/token changes. | PVC render changes from `syncthing-config` to `syncthing` (data-loss risk). LoadBalancer service name stays `syncthing-sync`, but it has no pinned `loadBalancerIP`, so verify MetalLB/external IP does not churn. Ingress name stays `syncthing`. |
| vault | 3.5.1 | LOW | Values render on v5. HelmRelease name is `vault`, controller is `stash`; render kept Deployment/Service names stable as `vault`. Cross-cutting selector/ServiceAccount/token changes. Existing PVCs only. | No LB or Ingress. Existing PVCs only. Service name stays `vault`. |

## Recommended merge order

1. **Pilot: rreading-glasses** — stateless, no PVC, no Ingress, no LoadBalancer, single container, and easiest rollback/health check.
2. **Next low-risk stateless or existing-PVC web apps:** prowlarr, overseerr, bazarr.
3. **Remaining existing-PVC media apps:** radarr, sonarr, sabnzbd, calibre-web, bookshelf, phantom, vault. These should still be one PR per app, but they can be reviewed as a series because they share the same pattern.
4. **kindle-sender** — migrate after one or two low-risk successes because ServiceMonitor `jobLabel` behavior changes and metrics should be verified.
5. **homebox** — do late because its chart-managed PVC name changes. Decide whether to keep the old PVC name via values, migrate data, or intentionally adopt a new claim before applying.
6. **qbittorrent** — do late because the pod is operationally complex and depends on VPN sidecar behavior, port forwarding, and external tracker update logic.
7. **syncthing last** — highest blast radius: chart-managed PVC rename risk, LoadBalancer exposure, Ingress, and stateful synchronization workload.

Do not batch the first few migrations. After the pilot and two low-risk apps have been stable for 24 hours each, the remaining LOW-risk apps may be prepared as a small sequence of independent PRs, but still merge only one app at a time.

## Runbook for migrating one app

1. Create one branch per app, for example `blazepower/app-template-v5-rreading-glasses`.
2. Edit only that app's `apps/<app>/helmrelease.yaml`.
3. Keep the chart version pinned in the individual HelmRelease: set `spec.chart.spec.version` to the latest v5 patch, currently `5.0.1`. Do **not** move the version to a shared kustomize base; per-HR pins are what make one-app-at-a-time rollout possible.
4. Review app-specific value changes before rendering:
   - If the app relies on in-cluster Kubernetes API access, set the appropriate ServiceAccount and `automountServiceAccountToken: true`; otherwise keep the v5 default.
   - If the app has chart-managed PVCs (`homebox`, `syncthing`), preserve/adopt the existing claim name before applying.
   - If the app has `serviceMonitor`, explicitly set `jobLabel` if dashboards/alerts expect the old value.
   - If any future app uses `rawResources`, move manifest content under `rawResources.<name>.manifest` before v5.
5. Render locally with Helm `>=3.18` and Kubernetes version matching the cluster, for example:

   ```powershell
   helm repo add bjw-s https://bjw-s-labs.github.io/helm-charts --force-update
   helm repo update bjw-s
   # Extract spec.values for the target app into a local values file, then render:
   helm template <release> bjw-s/app-template --version 5.0.1 --namespace <namespace> -f <values-file> --kube-version 1.31.0
   ```

6. Compare the rendered v3 and v5 manifests. At minimum verify resource names, Deployment selector changes, Service selectors, ServiceAccount, PVC names, Ingress names, and ServiceMonitor labels.
7. Commit and open a PR for that single app. In the PR body, include the render summary and expected resource churn.
8. After merge, let Flux apply the change. Watch HelmRelease, Deployment, pods, Services, PVCs, Ingress, and app health:

   ```powershell
   flux get hr -n <namespace> <release>
   kubectl -n <namespace> get deploy,po,svc,pvc,ingress -l app.kubernetes.io/instance=<release>
   kubectl -n <namespace> describe helmrelease <release>
   ```

9. Exercise the app's user-facing function. For media apps, verify config persistence and access to `media-root`. For monitoring apps, verify Prometheus targets. For LoadBalancer apps, verify the external IP and client connectivity.
10. Wait at least 24 hours before merging the next app-template v5 migration.

## Rollback procedure

1. Revert the single-app migration commit or set that app's `spec.chart.spec.version` back to `3.5.1`.
2. If Flux/Helm failed because Deployment selectors are immutable, delete only the affected Deployment after confirming the old values are restored; then reconcile the HelmRelease so it recreates the v3 Deployment.
3. For PVC-risk apps, do **not** delete any PVC until data ownership is confirmed. If v5 created a new claim (`homebox` or `syncthing`), scale the workload down and re-point the chart back to the known-good claim or restore data from backup before proceeding.
4. For LoadBalancer or Ingress issues, verify Service names and external IPs after rollback, then reconcile DNS/clients only if the IP actually changed.
5. Confirm the app is healthy on v3 before attempting another migration PR.

## Guardrails

- This plan intentionally does not modify any HelmRelease files.
- Keep chart versions pinned per HelmRelease so migrations can proceed and roll back one app at a time.
- Treat CI `kustomize-build` as syntax coverage only for these migrations; it does not template Flux HelmReleases and cannot prove bjw-s chart compatibility.
- Each app migration PR should include a rendered manifest summary and a clear statement of expected resource churn.
