# Charts and rendering conventions

This repo uses [bjw-s `app-template`](https://github.com/bjw-s/helm-charts)
for most application workloads, and raw manifests for the rest. This page
documents **when to use which**, and the gotchas we've already paid for
once and don't want to pay for again.

## TL;DR

- **Default to bjw-s `app-template`** for any "boring" app: one container,
  one config PVC, one service, one ingress.
- **Drop to raw manifests + kustomize** when the app needs host devices,
  multi-PVC topologies that don't fit `persistence`, or anything that
  makes you fight the template (e.g. Plex).
- **Never bump the bjw-s chart version on autopilot.** It is pinned per
  HelmRelease and bumped quarterly with explicit testing.
- **One persistence key per PVC.** Do not declare the same `existingClaim`
  under multiple keys — see [Gotcha #1](#gotcha-1-one-persistence-key-per-pvc).

---

## When to use bjw-s `app-template`

Use it when the app fits this shape:

- Single (or a small set of) container(s)
- A `config` PVC and/or one media PVC
- A ClusterIP service
- A standard ingress (or two: internal + tailscale)
- A couple of env vars from configmaps/secrets
- Common operational sugar: probes, reloader annotations, common labels

Examples that fit cleanly: `sonarr`, `radarr`, `bazarr`, `prowlarr`,
`bookshelf`, `kindle-sender`, `syncthing`, `calibre-web`, `sabnzbd`,
`qbittorrent` (with gluetun sidecar), `overseerr`.

Why bother: it gives you sidecars, probes, reloader, labels, ingress and
network policies "for free" without 3k lines of duplicated YAML across
~15 apps.

## When NOT to use bjw-s `app-template`

Use raw manifests + kustomize when **any** of these are true:

- The app needs `hostPath` device passthrough (e.g. `/dev/dri` for Plex
  HW transcoding, `/dev/snd`, GPU device plugins).
- The pod spec needs raw `extraVolumes`/`extraVolumeMounts` patterns that
  the chart's `persistence.*` shape can't express.
- The app is a `StatefulSet` with non-trivial volume claim templates
  (the chart supports StatefulSets but they're awkward beyond 1 PVC).
- The app needs a sidecar with very specific shared mount paths,
  emptyDir tmpfs sizes, or capabilities that fight the chart defaults.

Today this list is just `plex`. Keep it that way unless we have a real
reason to add to it.

## Folder layout

```
apps/<app-name>/
├── helmrelease.yaml         # bjw-s OR raw chart, your call
├── configmap.yaml           # non-secret env (PUID, PGID, TZ)
├── pvc.yaml                 # app-specific config PVC (Longhorn)
├── certificate.yaml         # cert-manager Certificate
├── ingress.yaml             # internal Traefik ingress (HTTPS, mTLS)
├── ingress-<app>-tailscale.yaml  # optional tailscale external ingress
├── kustomization.yaml       # ties the above together
└── README.md                # what does this app do, how to debug
```

Raw-manifest apps follow the same layout but replace `helmrelease.yaml`
with a `deployment.yaml` / `statefulset.yaml`.

---

## Gotchas (paid in production)

### Gotcha #1: one persistence key per PVC

**Symptom:** pod stays in `ContainerCreating` for 15-20 min with only a
`Scheduled` event. csi-driver-nfs reports the mount succeeded almost
immediately, but kubelet never proceeds to image pull or container
creation. `pod_workers.go` logs:

```
Error syncing pod, skipping  err="unmounted volumes=[media-movies media-tv]: context deadline exceeded"
```

**Cause:** bjw-s renders one `spec.volumes[]` entry per `persistence`
key. When multiple keys reference the same PVC:

```yaml
# ❌ DO NOT DO THIS
persistence:
  media-tv:
    existingClaim: media-root
    globalMounts: [{ path: /media/TV Shows, subPath: tv }]
  media-movies:
    existingClaim: media-root
    globalMounts: [{ path: /media/Movies, subPath: movies }]
```

…kubelet de-duplicates by `uniqueVolumeName` (derived from the PVC, not
the spec.volumes name) and performs only ONE `NodePublishVolume`. The
CSI mount succeeds, but `volumeManager.WaitForAttachAndMount(pod)` keeps
polling for **every** entry in `spec.volumes` to be reported as mounted.
Only one ever is, so it loops until the 2-min pod-sync timeout fires —
forever.

**Fix:** one persistence key, many mounts.

```yaml
# ✅ DO THIS
persistence:
  media:
    existingClaim: media-root
    globalMounts:
      - { path: /media/TV Shows, subPath: tv }
      - { path: /media/Movies,   subPath: movies }
```

Same rule applies to `advancedMounts` (qbittorrent's sidecar pattern).

History: PR #149 fixed this across bazarr, sabnzbd, sonarr, radarr,
qbittorrent, whisparr.

### Gotcha #2: chart version bumps are a release event

bjw-s `app-template` ships breaking changes between minors (e.g. v2→v3
reshaped the values schema). **Do not let Renovate auto-bump it.**

- Chart is pinned per-HelmRelease at `chart.spec.version: "3.5.1"`.
- Bump quarterly: pick one app, render with `helm template`, diff
  against the current rendered manifest, fix any drift, then bump the
  rest.
- Renovate config explicitly excludes the bjw-s chart from automerge.

### Gotcha #3: image tag bumps need an actual working image

Renovate has surprised us by bumping image tags to versions whose OCI
config has no `ENTRYPOINT`/`CMD` (e.g. `linuxserver/calibre-web:5.33.2`
in PR #146, reverted in PR #148). Pod will fail to start with
`failed to generate spec: no command specified`.

- Pin major versions in `renovate.json` for `linuxserver/*` images.
- Watch the renovate dashboard before merging image bumps to critical
  paths (anything *arr or Plex).

### Gotcha #4: `globalMounts` vs `advancedMounts`

- `globalMounts`: applied to every container of every controller in the
  release. Fine for single-container apps.
- `advancedMounts`: scoped to specific controllers and/or containers.
  Required when you have sidecars that should NOT see the media mount
  (e.g. gluetun, vpn-sync curl sidecar in qbittorrent).

If you use `globalMounts` on a multi-container release by mistake, every
sidecar will get every mount — usually harmless but occasionally breaks
read-only/permissions assumptions.

### Gotcha #5: `defaultPodOptions.nodeSelector` pins the whole release

We pin most media apps to `rishik-worker1` because the CSI NFS mount,
GPU transcoding, and large pull cache all live there. Be aware:
- Worker1 has a default pod cap of **110**. Migration churn has hit
  the cap before and made new pods `Pending` with `Too many pods`.
- Always clean up Completed migration/cronjob pods before a cutover.

---

## Debugging recipes

### "What did the chart actually render?"

```sh
helm get manifest -n <ns> <release>
```

Compare against what's running:

```sh
kubectl get deploy -n <ns> <release> -o yaml | yq '.spec.template.spec'
```

Server-side-apply can leave **orphan** volumes in the Deployment spec
that the chart no longer emits. These are harmless if no container
`volumeMounts` reference them — but they will confuse you. To force a
clean apply:

```sh
flux suspend hr -n <ns> <release>
kubectl delete deploy -n <ns> <release>
flux resume hr -n <ns> <release>
```

(This causes a brief outage, so only when stuck.)

### "Why is my pod stuck in ContainerCreating?"

In order, check:

1. `kubectl describe pod -n <ns> <pod>` — look for `FailedMount`,
   `FailedAttach`, image pull errors. If only `Scheduled` + kyverno
   `PolicyViolation` events appear, kubelet is not making progress.
2. `kubectl logs -n csi-driver-nfs <csi-nfs-node pod on the node>` —
   confirm `NodePublishVolume` succeeded.
3. Goroutine dump of kubelet:
   ```sh
   kubectl get --raw "/api/v1/nodes/<node>/proxy/debug/pprof/goroutine?debug=2"
   ```
   Search for `WaitForAttachAndMount` — if there are 3+ goroutines stuck
   there, you've hit [Gotcha #1](#gotcha-1-one-persistence-key-per-pvc).
4. journalctl on the node (via `kubectl debug node/<node>`):
   ```sh
   tail -200 /host/var/lib/rancher/rke2/agent/logs/kubelet.log
   ```

### "How do I break a helm upgrade↔rollback loop?"

If a HelmRelease keeps cycling between upgrade and rollback (15min
timeouts each), short-term patch:

```sh
kubectl patch hr -n <ns> <release> --type=merge -p '{
  "spec":{
    "upgrade":{"timeout":"30m","remediation":{"retries":0}},
    "install":{"timeout":"30m","remediation":{"retries":0}}
  }
}'
```

Then either fix the underlying issue or `flux suspend hr` to freeze it.

If the helm release itself is stuck in `pending-upgrade`:

```sh
helm history -n <ns> <release>
helm rollback -n <ns> <release> <last-deployed-revision>
flux reconcile hr -n <ns> <release>
```

### "Why does kyverno spam PolicyViolation events?"

Most of our policies are in `Audit` mode — they generate events but do
NOT block admission. Only these enforce-mode policies actually block:

- `block-risky-capabilities`
- `disallow-host-namespaces`
- `restrict-host-ports`
- `enforce-namespace-labels`

The `disallow-latest-tag` JMESPath errors you see are a kyverno regex
bug, not a real violation. See [docs/policies.md](policies.md) for the
full enforce/audit matrix.

---

## Files

- `apps/*/helmrelease.yaml` — per-app chart releases
- `apps/plex/helmrelease.yaml` — example of the raw-manifest pattern
- `apps/nfs-media/` — shared `media-root` PVC + PV definitions
- `infrastructure/csi-driver-nfs/` — csi-driver-nfs install
- [docs/storage.md](storage.md) — Longhorn + NFS layout
- [docs/policies.md](policies.md) — kyverno policy modes
