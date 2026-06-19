# Tailscale exposure audit (2026-06-18)

## Context

`worker1` hit the kubelet default `maxPods=110` cap, leaving overseerr, bookshelf,
kured, kube-prometheus-stack-operator, media-mount-handler, and trivy scans stuck
in `Pending`. PR #151 unpinned non-GPU workloads from `worker1` ~2 weeks ago, but
because `controller` is intentionally cordoned (`SchedulingDisabled`), every pod
piles onto `worker1`.

This PR raises `worker1` `maxPods` to **250** via a GitOps-managed RKE2 kubelet
config snippet (mirrors the `iscsi` and `gpu` bootstrap pattern). That alone
restores headroom. The remaining work below is optional follow-up.

## Current Tailscale-operator footprint

| Pod count | Source |
| --------- | ------ |
| 20        | per-app `ts-*-tailscale-*` StatefulSets (one per Ingress with `ingressClassName: tailscale`) |
| 1         | `homelab-ingress` ProxyGroup (`type: egress`, 2 replicas — actually 1 currently) |

Every app under `apps/` has **both** a public-facing `ingress.yaml` and a
tailnet-only `ingress-<app>-tailscale.yaml`. That dual-exposure is intentional;
removing it isn't desirable. What's wasteful is that the Tailscale operator
spawns a dedicated 1-replica StatefulSet per Ingress instead of fanning out
through a shared proxy.

## Recommended follow-up: consolidate onto a single ingress ProxyGroup

The Tailscale Kubernetes operator supports `ProxyGroup` of `type: ingress`
(GA since operator v1.74). One `ProxyGroup` with N replicas can serve many
Ingresses by annotating each Ingress with
`tailscale.com/proxy-group: <name>`. Net effect:

- 20 per-app StatefulSets → 2-3 shared replicas (≈85% pod reduction in the
  `tailscale` namespace).
- HA tailnet ingress (single-replica per-app StatefulSets are SPOFs today).
- No change to which apps are exposed on the tailnet.

Concrete steps (deferred — out of scope for this PR):

1. Add a second `ProxyGroup` named e.g. `homelab-tsingress` with `type: ingress`,
   `replicas: 2` in `infrastructure/tailscale/proxygroup.yaml`.
2. Annotate each `ingress-<app>-tailscale.yaml` with
   `tailscale.com/proxy-group: homelab-tsingress`.
3. Roll one app at a time, verify tailnet DNS still resolves, then prune the
   per-app `ts-*-tailscale-*` StatefulSets that the operator orphans.

## Side-by-side: per-Ingress vs ProxyGroup

| | per-Ingress (today) | ProxyGroup (proposed) |
|---|---|---|
| Pods on worker1 | 20 | 2 |
| HA | None (1 replica each) | Yes |
| Operator version required | Any | ≥ v1.74 |
| Configuration change per app | None | One annotation line |

## Why not just delete tailscale ingresses?

We considered culling tailnet exposure for low-traffic apps (cooklang,
homebox, kaneo, paperless, etc.). Decided against it — the user actively uses
tailnet access and per-app removals would surprise them. Consolidation gives
the same pod-count win without removing functionality.
