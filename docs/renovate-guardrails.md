# Renovate guardrails & update-safety runbook

This doc captures the guardrails we've added to `renovate.json` to prevent a
class of outage where a "newer" tag chosen by Renovate is actually broken
(wrong architecture, missing entrypoint, orphaned old tag, etc.), and how to
recover when helm-controller has latched onto a bad release.

## Background: the incident (July 2026)

Two workloads were simultaneously in a failed state:

| App          | Symptom                                              | Bad tag                                       |
|--------------|------------------------------------------------------|-----------------------------------------------|
| plex         | `exec /init: exec format error`, CrashLoopBackOff    | `plexinc/pms-docker:1.43.2.10687-563d026ea-armhf` |
| calibre-web  | `failed to generate spec: no command specified`      | `lscr.io/linuxserver/calibre-web:5.33.2`      |

### Root causes

1. **Plex** — `plexinc/pms-docker` publishes per-arch tags where the amd64
   variant is the *bare* tag (`1.43.2.10687-<sha>`) and other architectures
   use suffixes (`-armhf`, `-arm64`, `-arm64v8`, `-aarch64`). Renovate has no way to
   know a `-armhf` suffix means "different architecture" rather than
   "newer build metadata", so PR #166 updated our (amd64) cluster to the
   ARMv7 image, which fails immediately at exec time.
2. **calibre-web** — LinuxServer's real release stream is `0.6.x`
   (current: `0.6.26`). A handful of `5.x` tags exist from a 2021
   experiment; the images are amd64-only and have no `Entrypoint`/`Cmd` in
   their OCI config, so containerd can't build a spec. Semver-wise
   `5.33.2 > 0.6.24`, so PR #146 happily crossed a major boundary into a
   broken orphan tag.

### Why the cluster stayed broken after pinning

Commit `7b4da3a` pinned calibre-web back to `0.6.24` in git, but the live
HelmRelease continued to run `5.33.2`. Helm-controller's install of the
pinned version was timing out on readiness probes; each failed upgrade
triggered `Remediated: RollbackSucceeded` back to release `v23` — which
held `tag: 5.33.2`. Every reconcile therefore restored the broken image.

## Guardrails now in `renovate.json`

- **Plex `allowedVersions`** rejects any tag ending in an architecture
  suffix (`-armhf`, `-arm64`, `-arm64v8`, `-arm32v7`, `-arm32v6`,
  `-aarch64`, `-amd64`). Only the bare (amd64) tag stream is considered.
- **calibre-web uses one final positive `allowedVersions` regex** that accepts
  only numeric `0.x.y` releases with an optional LinuxServer `-lsNNN` build
  suffix. This simultaneously excludes the orphaned `5.33.2`/`6.x` streams
  and architecture-prefixed or architecture-suffixed tags. It intentionally
  comes after the generic LinuxServer rule because Renovate merges matching
  package rules in order and later scalar values such as `allowedVersions`
  win.
- **LinuxServer `allowedVersions`** rejects common architecture prefixes and
  suffixes (`amd64`, `arm64v8`, `arm32v7`, `arm32v6`, `aarch64`, `arm64`,
  `arm`, `i386`, `x86_64`) so Renovate does not select a single-arch tag.
- **LinuxServer major bumps require Dependency Dashboard approval** — their
  tagging scheme varies per image and majors have burned us before.
- **Prereleases are ignored globally.** Sure Finance is pinned to an alpha
  today, so its package rule additionally rejects explicit prerelease channel
  markers (`alpha`, `beta`, `rc`, `preview`, etc.). The rule is package-scoped
  and marker-aware rather than rejecting every hyphenated Docker tag, so stable
  compatibility/build tags such as `-alpine`, `-bookworm`, and `-ls123` remain
  eligible.
- **Flux bootstrap controllers are upgraded as one official Flux release.**
  Individual image updates in `gotk-components.yaml` are disabled; regenerate
  the complete bundle, including matching CRDs and RBAC, when reviewing a new
  stable Flux release. This does not disable other images or Helm updates.
- **Maintenance kubectl stays on the cluster's current `1.34.x` minor.**
  Patch and digest updates remain eligible. Review the `alpine/k8s` ceiling
  with each Kubernetes minor upgrade; Kubernetes only supports kubectl within
  one minor of the API server, so `1.37.x` is not safe against this cluster.
- **Infrastructure charts have independent PRs**, rather than one bundle
  coupling admission, storage, networking and scheduling controllers.
- **Longhorn chart automation is held at the existing exact pin.** Resume it
  only after a dedicated storage upgrade review covers backup/restore,
  engine versions, replica health and maintenance capacity. This hold does
  not disable other infrastructure charts or require replacing the accepted
  worn SSD.
- **Descheduler remains on `0.36.x` with Kubernetes `1.34`.** The `0.37`
  release tests the three newest Kubernetes minors (`1.35` through `1.37`),
  excluding this API server. Reassess this ceiling, eviction policy, PDBs
  and maintenance capacity during node-minor upgrade planning.
- **Calibre-Web image updates retain digests**, including the library-bootstrap
  init container. The existing bootstrap test still requires its image to match
  the application image exactly.

> **PR #192 regression:** PR #187 added the Plex suffix denylist as
> `!/(armhf|arm64v8|aarch64|arm)$/`, but omitted the exact `arm64`
> alternation. Because `$` anchored immediately after `arm`, `-arm64` did not
> match either `arm` or `arm64v8`, and Renovate opened PR #192 for the
> arm64-only tag `1.43.2.10687-563d026ea-arm64`. The guard now includes the
> full suffix list and is covered by automated tests.

## Automated guardrail tests

`.github/scripts/test-renovate-guards.py` parses `renovate.json`, models the
ordered effective `allowedVersions` value for each package, and verifies
known-bad arch variants and calibre-web `5.33.2`/`6.x` tags are rejected while
normal tags and valid calibre-web `0.6.x` LinuxServer tags are allowed. CI runs
it as the fast `renovate-guardrails-test` job.

## When to add more guardrails

Consider a per-package rule whenever an image:

- publishes arch-specific tag suffixes (Plex, some `hotio/*` images);
- has multiple parallel release streams with numeric collisions (calibre-web);
- uses a rolling tag (`latest`, `stable`, `edge`) — prefer
  `pinDigests: true` on those so architecture/entrypoint drift shows up as
  a sha256 change in the PR diff.

## November 2026 PR dispositions

The objective is **safely verified merges**, not an empty PR queue. On November 6
the owner clarified that missing verification, a needed chart bump or another
prerequisite is work to complete on an open PR, not grounds for closing it.
The ten PRs prematurely closed on November 5 are being reopened, and the six
approval rules added solely to defer them are removed. A reopened PR is not
an assertion that its upgrade is safe or deployed.
[Issue #359](https://github.com/blazepower/rishik-homelab-rke2/issues/359)
tracks the remaining recovery and release work alongside the individual PRs.

The dashboard is [#164](https://github.com/blazepower/rishik-homelab-rke2/issues/164).
Complete backup/restore, compatibility and rollout checks rather than hiding
unfinished work behind new Dashboard approval rules. Keep unready PRs open.
Close only as a last resort for an obsolete, duplicate, retired or demonstrably
unsuitable update, with a specific justification. Preserve existing
architecture/version guards, coordinated Flux and Longhorn rules, CWA approval
and explicit live maintenance/CA holds. Releasing a live hold requires its own
review; reopening a PR does not release it. The accepted worn SSD is not a
blanket replacement prerequisite.

### This backlog

| PR | Disposition | Justification and next gate |
|---|---|---|
| #354 | Merge | Exact old/new Ubuntu images execute only the existing completed-marker path on all three nodes; script, host configuration, GPU allocation and Plex identities must remain unchanged. |
| #356 | Merge | Maintenance kubectl patch stays on the server's 1.34 minor; validate native client/tool compatibility, unchanged handler script/RBAC and the actual scheduled job. |
| #362 | Merge | The scheduled maintenance check exposed a preexisting false restart of native CWA at `/media`. Remove that obsolete target, retain all other checks and verify the next scheduled job preserves CWA uptime; repair the stale source-readonly wiring test without weakening the bootstrap guards. |
| #291 | Reopen; complete verification | Newer Plex image needs its own fresh consistent library/configuration backup, isolated restore, exact CA/runtime audit and controlled GPU/media-preserving rollout. Current Plex is stable, not proven safe to migrate. |
| #336 | Reopen; complete verification | PostgreSQL 18 chart defaults require separate Maybe/Paperless native recovery proofs and local chart version bumps; changing defaults alone can leave Flux using a cached chart. Keep Paperless paused. |
| #337 | Reopen; complete verification | rreading-glasses needs its own native database backup and candidate restore; successful Kaneo recovery does not prove this database's contents or extensions. |
| #344 | Reopen; complete verification | Persistent Redis is not disposable cache. Back up and restore its durable state/queues, verify consumers and bump the local charts before rollout. |
| #341 | Reopen; complete verification | Complete Grafana provisioning-parity recovery proof. Empty scratch provisioning orphan-deleted dashboards in both versions; the later 127 MB capture timed out. These are test limitations, not production loss or proof of a 13.3 defect. Preserve canonical dashboards/resources/users/datasources/permissions, Prometheus history and silences. |
| #342 | Reopen; complete verification | Kyverno HelmRelease and policy reconciliation are intentionally held. Review admission behavior, capacity and rollback before explicitly releasing either hold; preserve active policies. |
| #351 | Reopen; complete verification | The rolling Whisparr target changed during review and requires a fresh exact-image CA/runtime and recovery audit. Its live HelmRelease is not suspended; reopening does not change it. |
| #352 | Reopen; complete verification | The accepted-CA qBittorrent cutover was completed through #381/#382; its HelmRelease is no longer suspended. Complete exact old/new Linux helper-script verification and a controlled sidecar/CronJob-template rollout. This changes neither application/database images nor CA guard images, and does not require inventing a new CA cutover hold. |
| #358 | Reopen; complete verification | AdGuard 0.107.80 contains security fixes, but this proposal changes only a local chart default while leaving chart version/appVersion at 1.0.2/0.107.79. Coordinate a fresh native DNS configuration recovery proof, chart metadata bump and actual rollout instead of claiming a cached-chart no-op is deployed. |
| #360 | Reopen; complete verification | MetalLB 0.16.2 is not merely an image rebuild: the upstream comparison contains L2 election and CRD/schema changes. Review current pool/advertisement validation, upgrade's existing CRD `Create` policy and address/election continuity before replacing the load-balancer components. |

### Earlier unmerged closures in this upgrade campaign

These are separate from successfully merged upgrades. Existing PR comments
retain detailed evidence; the table supplies a justification for every unmerged
closure since October 25, including Renovate's automatic closures.

| PR | Closure justification | Renovate handling |
|---|---|---|
| #353 | Verified redundant Hardcover rebuild: application and every changed layer's file contents/permissions/configuration match the deployed image; only build timestamps/OCI metadata differ. | No blanket suppression: future application or trust-store updates must remain eligible. |
| #338 | Owner retired a completed one-shot Grafana preservation helper update; not a live monitoring image upgrade. | No blanket sidecar exclusion; chart/runtime updates remain independently reviewable. |
| #328 | Ubuntu major bump targeted the unused Cook Desktop sync component, subsequently retired while preserving recipes/archive data. | Removed component is no longer extracted; do not constrain unrelated Ubuntu images. |
| #101 | Owner closed the stale, conflicted calibre-converter feature proposal; do not merge obsolete architecture over the current native EPUB ingestion path. | Not a dependency update; no Renovate rule applies. |
| #102 | Owner closed the stale, conflicted Tailscale documentation proposal rather than replacing current documentation wholesale. | Not a dependency update; no Renovate rule applies. |
| #236 | Broad infrastructure bundle was split to avoid coupling incompatible admission/storage/networking changes; cert-manager was reviewed and deployed separately through #329. | Independent infrastructure rules; retain the dedicated Longhorn hold. |
| #327 | Renovate auto-closed the old `major-infrastructure` Traefik branch during regrouping. Traefik 42 was not deployed by that closure. | Keep independent Traefik review eligible; do not invent an unsupported-version ban. |
| #331 | Descheduler 0.37's tested Kubernetes range excludes the current 1.34 cluster. | Existing 0.36.x ceiling, reconsidered with a node minor upgrade. |
| #330 | Superseded by the separately reviewed compatible cert-manager 1.21.3 change in #329. | No additional exclusion; independent cert-manager upgrades remain eligible. |
| #316 | Individual Flux source-controller bump would bypass coordinated bootstrap/CRD/RBAC upgrades. | Existing generated-bootstrap-only controller rule; official bundle upgraded through #319. |
| #308 | Individual Flux notification-controller bump would bypass the coordinated release. | Same narrow bootstrap rule; no global Flux-image suppression. |
| #307 | Individual Flux kustomize-controller bump would bypass the coordinated release. | Same narrow bootstrap rule. |
| #306 | Individual Flux helm-controller bump would bypass the coordinated release. | Same narrow bootstrap rule. |
| #270 | kubectl 1.37 violates supported skew against the Kubernetes 1.34 API. | Existing 1.34.x maintenance-client ceiling; compatible #356 patch remains eligible. |
| #318 | Not a real Calibre upgrade: removed only the bootstrap image digest, breaking immutable app/bootstrap identity equality. | Existing digest pin and equality test; later native CWA migration is separate. |
| #239 | Independent Kaneo chart update was superseded by the coordinated chart/application/database migration in #309. | No extra exclusion; obsolete references were replaced together. |
| #240 | Independent Kaneo API update was superseded by the matching coordinated migration in #309. | Same coordinated manifest change, not a permanent package ban. |
| #241 | Independent Kaneo web update was superseded by the matching coordinated migration in #309. | Same coordinated manifest change. |

## Recovery: bad tag stuck via helm-controller rollback loop

If a HelmRelease keeps rolling back to a release that itself contains the
bad image (as happened with calibre-web), the fix in git is not enough.
Break the loop manually:

```powershell
# 1. Stop Flux from fighting you.
flux -n <ns> suspend hr <release>

# 2. (Recommended) Set spec.upgrade.remediation.retries: 0 on the
#    HelmRelease before resuming, so if the git-pinned version still
#    can't converge for an unrelated reason (bad probe, missing PVC,
#    etc.) the upgrade fails loudly instead of rolling back into the
#    same broken revision that caused the loop. Optionally also set
#    spec.upgrade.remediation.strategy: uninstall.

# 3. Find the last good revision (one whose values had a working tag).
helm -n <ns> history <release>

# 4. Roll back to that revision. This overwrites the "previous release"
#    slot that helm-controller would otherwise remediate to. Helm marks
#    all prior revisions "superseded" when a new revision starts, so if
#    you deleted the pending secret manually you may need to `helm
#    rollback` to the last known-deployed rev just to restore a
#    `deployed`-status entry in history.
helm -n <ns> rollback <release> <good-rev>

# 5. Directly patch the workload image to a known-good tag if the
#    rolled-back release still points at the bad image (kubectl set
#    image sts/... or deploy/...). This bypasses helm entirely and
#    heals the pod immediately.

# 6. Confirm pods are healthy, then let Flux take over again.
kubectl -n <ns> get pods -l app.kubernetes.io/instance=<release>
flux -n <ns> resume hr <release>
flux -n <ns> reconcile hr <release> --with-source
```

Note: `HelmRelease.spec.chart.spec.version` must be a version helm-controller
can actually install successfully. If the chart version in git differs from
what you rolled back to, the next reconcile will attempt an upgrade
regardless — so make sure the pinned image tag is compatible with the
chart version that will be applied.

## Related gotcha

When editing a locally-vendored chart under `charts/<chart>/`, bump
`charts/<chart>/Chart.yaml` `version` as well — otherwise Flux
source-controller reuses its cached chart tarball and the HelmRelease
no-ops. (See PR #173 / paperless-ngx `1.0.5 -> 1.0.6`.)
