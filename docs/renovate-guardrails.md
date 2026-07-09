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

# 2. Find the last good revision (one whose values had a working tag).
helm -n <ns> history <release>

# 3. Roll back to that revision. This overwrites the "previous release"
#    slot that helm-controller would otherwise remediate to.
helm -n <ns> rollback <release> <good-rev>

# 4. Confirm pods are healthy, then let Flux take over again.
kubectl -n <ns> get pods -l app.kubernetes.io/instance=<release>
flux -n <ns> resume hr <release>
flux -n <ns> reconcile hr <release> --with-source
```

For releases that repeatedly fail to converge, also consider setting
`spec.upgrade.remediation.retries: 0` (and/or `spec.upgrade.remediation.strategy: uninstall`)
on the HelmRelease so a bad upgrade fails loudly instead of oscillating
back to the previously-broken revision.

## Related gotcha

When editing a locally-vendored chart under `charts/<chart>/`, bump
`charts/<chart>/Chart.yaml` `version` as well — otherwise Flux
source-controller reuses its cached chart tarball and the HelmRelease
no-ops. (See PR #173 / paperless-ngx `1.0.5 -> 1.0.6`.)
