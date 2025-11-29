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

**Mutation**: Adds node-aware scheduling (topology spread constraints across nodes) with `maxSkew: 1` and `whenUnsatisfiable: ScheduleAnyway`.

### 5. Mutate Ingress Annotations

**Policy**: `mutate-ingress-annotations`

Adds standard annotations to Ingress resources:
- `traefik.ingress.kubernetes.io/router.tls: "true"`
- `homelab.rishik.dev/managed-by: "kyverno"`
- `homelab.rishik.dev/log-level: "info"`

### 6. Disallow Latest Tag

**Policy**: `disallow-latest-tag`

Enforces image hygiene by:
- Blocking images tagged `:latest`
- Blocking images without explicit tags
- Requiring images from trusted registries: `docker.io`, `ghcr.io`, `quay.io`, `gcr.io`, `registry.k8s.io`, `mcr.microsoft.com`, `lscr.io`

Docker Hub library images (e.g., `nginx:1.21`) are allowed.

### 7. Require Image Signatures

**Policy**: `require-image-signatures`

Verifies that container images are signed using Cosign/Sigstore for supply chain security:
- Uses keyless signature verification with Sigstore's Rekor transparency log
- Reports unsigned images in policy reports without blocking deployments (Audit mode)
- Helps identify and track unsigned images for future remediation

**Excludes**: `kube-system`, `kube-public`, `kube-node-lease`, `kyverno`, `flux-system`, `cert-manager`, `longhorn-system`, `storage`, `monitoring`

**Note**: This policy uses `required: false` initially. Once all images are signed, set `required: true` and transition to Enforce mode for full supply chain security.

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
├── cluster-policies/
│   ├── kustomization.yaml
│   ├── require-resources-limits.yaml
│   ├── block-risky-capabilities.yaml
│   ├── enforce-namespace-labels.yaml
│   ├── mutate-topology-spread.yaml
│   ├── mutate-ingress-annotations.yaml
│   ├── disallow-latest-tag.yaml
│   └── require-image-signatures.yaml
├── kyverno/
│   ├── kustomization.yaml
│   ├── disallow-host-namespaces.yaml
│   ├── restrict-host-ports.yaml
│   ├── restrict-capabilities.yaml
│   ├── disallow-privilege-escalation.yaml
│   ├── require-run-as-nonroot.yaml
│   ├── restrict-seccomp.yaml
│   └── restrict-apparmor.yaml
├── network-policies/
│   ├── kustomization.yaml
│   ├── default-deny.yaml
│   ├── allow-dns.yaml
│   ├── allow-ingress-controller.yaml
│   ├── allow-monitoring.yaml
│   └── allow-prometheus-internal.yaml
└── rwx-access/
    ├── kustomization.yaml
    ├── rwx-clusterrole.yaml
    └── rwx-clusterrolebinding.yaml
```

## Pod Security Standards (Baseline)

These policies implement the Kubernetes Pod Security Standards baseline profile using Kyverno ClusterPolicies, complementing the existing "Cluster Policies" (particularly the `block-risky-capabilities` policy) by providing additional, fine-grained controls at the pod level to strengthen overall cluster security.

> **Important:** All policies are shown in **Audit** mode for safe testing. For production clusters, you **must** set `validationFailureAction: Enforce` for all critical pod security policies (privileged, host namespace/network, hostPath, capability, non-root, seccomp/AppArmor restrictions) to ensure enforcement and prevent privileged pod creation. You may use environment-based overrides to allow Audit mode in non-production environments, but production clusters must enforce these controls.

### 8. Disallow Host Namespaces

**Policy**: `disallow-host-namespaces`

Blocks sharing of host PID and IPC namespaces:
- `hostPID: true` - Allows container to access host process IDs
- `hostIPC: true` - Allows container to access host IPC resources

**Note**: `hostNetwork` is already blocked by the `block-risky-capabilities` policy.

### 9. Restrict Host Ports

**Policy**: `restrict-host-ports`

Blocks containers from binding to host ports, which could allow network traffic snooping or circumvent network policies.

### 10. Restrict Capabilities

**Policy**: `restrict-capabilities`

Enforces Linux capability restrictions:
- Requires containers to drop ALL capabilities
- Allows only safe capabilities to be added: `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `FSETID`, `KILL`, `SETGID`, `SETUID`, `SETPCAP`, `NET_BIND_SERVICE`, `SYS_CHROOT`, `SETFCAP`

### 11. Disallow Privilege Escalation

**Policy**: `disallow-privilege-escalation`

Requires `allowPrivilegeEscalation: false` on all containers to prevent processes from gaining more privileges than their parent.

### 12. Require Run as Non-Root

**Policy**: `require-run-as-nonroot`

Requires either pod-level or container-level `runAsNonRoot: true` to ensure containers don't run as the root user.

### 13. Restrict Seccomp

**Policy**: `restrict-seccomp`

Restricts seccomp profiles to `RuntimeDefault` or `Localhost`. Blocks `Unconfined` profiles which disable seccomp filtering.

### 14. Restrict AppArmor

**Policy**: `restrict-apparmor`

Restricts AppArmor profiles to `runtime/default` or `localhost/*`. Blocks `unconfined` profiles.

## Network Policies

Kubernetes NetworkPolicies for namespace-level network isolation. These policies are applied to the `default` namespace as templates. Copy them to other namespaces as needed.

### Default Deny All

**Policy**: `default-deny-all`

Denies all ingress and egress traffic by default. Apply to namespaces that need network isolation.

### Allow DNS

**Policy**: `allow-dns`

Allows egress to kube-dns (UDP/TCP port 53) for DNS resolution. Essential for pods to resolve service names.

### Allow Ingress Controller

**Policy**: `allow-ingress-controller`

Allows ingress from Traefik ingress controller in `kube-system` namespace. Required for services exposed via Ingress resources.

### Allow Monitoring

**Policy**: `allow-monitoring-scrape`

Allows ingress from the `monitoring` namespace on common application metrics ports (8080, 8443, 9100, 9090). Required for Prometheus to scrape application and node metrics.

### Allow Prometheus Internal

**Policy**: `allow-prometheus-internal`

Allows ingress within the `monitoring` namespace for Prometheus component-to-component communication. Covers ports for Prometheus server (9090), Pushgateway (9091), Alertmanager (9093, 9094), and Thanos (10901, 10902).

## RWX Access Controls

RBAC resources for managing ReadWriteMany (RWX) PersistentVolumeClaims with Longhorn storage.

### RWX PVC Manager ClusterRole

**ClusterRole**: `rwx-pvc-manager`

Provides permissions for:
- PVC creation, management, and deletion
- PV read access for debugging
- StorageClass read access
- Longhorn volume and share manager read access

### RWX PVC Manager Binding

**ClusterRoleBinding**: `rwx-pvc-manager-binding`

Binds the `rwx-pvc-manager` role to:
- `kustomize-controller` in `flux-system` namespace
- `helm-controller` in `flux-system` namespace
