# Policies - Kyverno

This document describes the Kyverno policy engine deployment and the cluster policies configured in the homelab.

## Overview

[Kyverno](https://kyverno.io/) is a Kubernetes-native policy engine that manages policies as Kubernetes resources. It provides:

- **Validation**: Ensure resources meet specific criteria before being created
- **Mutation**: Automatically modify resources during admission
- **Generation**: Create additional resources when certain conditions are met

## Installation

Kyverno is deployed via Flux HelmRelease in the `kyverno` namespace:

- **Version**: 1.16.0
- **Namespace**: `kyverno`
- **Components**: Admission Controller, Background Controller, Cleanup Controller, Reports Controller

## Resource Configuration

All Kyverno components have explicit resource requests and limits:

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Admission Controller | 100m | 500m | 256Mi | 512Mi |
| Background Controller | 50m | 200m | 128Mi | 256Mi |
| Cleanup Controller | 25m | 100m | 64Mi | 128Mi |
| Reports Controller | 25m | 100m | 64Mi | 128Mi |

## Cluster Policies

All policies are deployed in **Audit** mode for safe initial rollout. Change to `Enforce` mode after verifying policies work as expected.

### 1. Require Resources and Limits

**Policy**: `require-resources-limits`

Ensures all containers have CPU and memory requests/limits defined to prevent resource starvation.

**Excludes**: `kube-system`, `kube-node-lease`, `kube-public`, `flux-system`, `kyverno`, `longhorn-system`, `storage`, `monitoring`, `cert-manager`

### 2. Block Risky Capabilities

**Policy**: `block-risky-capabilities`

Blocks security-risky configurations in user namespaces:
- Privileged containers (`privileged: true`)
- Host networking (`hostNetwork: true`)
- Host path volumes (`hostPath`)

**Excludes**: Same as above (infrastructure namespaces need these capabilities)

### 3. Enforce Namespace Labels

**Policy**: `enforce-namespace-labels`

Requires namespaces to have:
- `environment` label (for environment identification)
- `owner` label (for ownership tracking)

Useful for Prometheus/Loki queries and future multi-tenant experiments.

**Excludes**: `default`, `kube-system`, `kube-node-lease`, `kube-public`, `flux-system`

### 4. Mutate Topology Spread

**Policy**: `mutate-topology-spread`

Automatically injects `topologySpreadConstraints` for Deployments to ensure workloads are spread across nodes.

**Mutation**: Adds zone-aware scheduling with `maxSkew: 1` and `whenUnsatisfiable: ScheduleAnyway`.

### 5. Mutate Ingress Annotations

**Policy**: `mutate-ingress-annotations`

Adds standard annotations to Ingress resources:
- `traefik.ingress.kubernetes.io/router.tls: "false"`
- `homelab.rishik.dev/managed-by: "kyverno"`
- `homelab.rishik.dev/log-level: "info"`

### 6. Disallow Latest Tag

**Policy**: `disallow-latest-tag`

Enforces image hygiene by:
- Blocking images tagged `:latest`
- Blocking images without explicit tags
- Requiring images from trusted registries: `docker.io`, `ghcr.io`, `quay.io`, `gcr.io`, `registry.k8s.io`, `mcr.microsoft.com`, `lscr.io`

Docker Hub library images (e.g., `nginx:1.21`) are allowed.

## Transitioning to Enforce Mode

To switch a policy from Audit to Enforce mode:

1. Monitor policy violations in the Kyverno Policy Reports
2. Address any violations in existing resources
3. Update the policy's `validationFailureAction` from `Audit` to `Enforce`

```yaml
spec:
  validationFailureAction: Enforce  # Changed from Audit
```

## Troubleshooting

### View Policy Reports

```bash
kubectl get policyreports -A
kubectl get clusterpolicyreports
```

### Check Policy Status

```bash
kubectl get clusterpolicies
kubectl describe clusterpolicy <policy-name>
```

### View Admission Logs

```bash
kubectl logs -n kyverno -l app.kubernetes.io/component=admission-controller
```

## File Structure

```
infrastructure/policies/
├── kustomization.yaml
├── namespace.yaml
├── helmrepository-kyverno.yaml
├── helmrelease-kyverno.yaml
└── cluster-policies/
    ├── kustomization.yaml
    ├── require-resources-limits.yaml
    ├── block-risky-capabilities.yaml
    ├── enforce-namespace-labels.yaml
    ├── mutate-topology-spread.yaml
    ├── mutate-ingress-annotations.yaml
    └── disallow-latest-tag.yaml
```
