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
   use suffixes (`-armhf`, `-arm64v8`, `-aarch64`). Renovate has no way to
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
  suffix (`-armhf`, `-arm64v8`, `-aarch64`, `-arm`). Only the bare (amd64)
  tag stream is considered.
- **calibre-web `allowedVersions: "<1.0.0"`** confines Renovate to the real
  upstream `0.6.x` stream.
- **LinuxServer major bumps require Dependency Dashboard approval** — their
  tagging scheme varies per image and majors have burned us before.
- **Flux controller images in `gotk-components.yaml` are disabled** —
  regenerate the whole Flux distribution instead of taking per-controller PRs.

## Flux distribution bumps

At the Flux 2.8→2.9 boundary, separate Renovate PRs tried to bump
helm-controller, kustomize-controller, and source-controller images while
leaving the generated CRDs and notification-controller on Flux 2.8.8. Avoid
that mixed-version state by closing per-controller PRs and opening one
coordinated PR generated with:

```powershell
flux install --export --version=<flux-vX.Y.Z> > clusters\production\flux-system\gotk-components.yaml
```

Review the generated CRD, RBAC, and controller image changes together.

## When to add more guardrails

Consider a per-package rule whenever an image:

- publishes arch-specific tag suffixes (Plex, some `hotio/*` images);
- has multiple parallel release streams with numeric collisions (calibre-web);
- uses a rolling tag (`latest`, `stable`, `edge`) — prefer
  `pinDigests: true` on those so architecture/entrypoint drift shows up as
  a sha256 change in the PR diff.

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
