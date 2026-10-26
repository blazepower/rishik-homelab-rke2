# Infrastructure update review: Kubernetes 1.34.12

This review replaces only the cert-manager patch from bundled Renovate PR #236
(reviewed head `10f5eb4df615e3b7d42c6763c33af6b761b8890a`).
Do not merge that bundle: a chart rendering successfully does not establish
runtime, admission, eviction, or storage-upgrade compatibility.

## Included: cert-manager v1.21.2 to v1.21.3

- The [official support matrix](https://cert-manager.io/docs/releases/) supports
  and tests cert-manager 1.21 on Kubernetes 1.33 through 1.36, including this
  cluster's three nodes at 1.34.12.
- The [patch release](https://github.com/cert-manager/cert-manager/releases/tag/v1.21.3)
  recommends upgrading all users. It removes compromised CA trust, fixes the
  ACME ARI renewal loop, and restricts consumption of next-private-key Secrets
  to Secrets labeled for that purpose and owned by their Certificate.
- Repository values, resource bounds, certificate owner-reference behavior,
  issuers, and Secret references are unchanged. The cluster uses `cluster-ca`;
  no issuer, private key, trust Secret, or certificate is replaced by this PR.

### Validation and blast radius

Both exact chart archives were rendered with the repository's `spec.values`,
namespace `cert-manager`, `--include-crds`, and `--kube-version 1.34.12`.
SHA-256:

| Chart | SHA-256 |
| --- | --- |
| v1.21.2 | `73a56e1728edd6c99f1f31082618c3259d279a76b7ebd3d4bdc5475c2442d34a` |
| v1.21.3 | `9ec67938b2981d8b333af61b3fcf53e26b4f6f5deb7ccda3de4ea116bb373a17` |

The renders contain the same 50 resource identities and six CRDs. Semantic
comparison found only version/chart labels, four component images, the ACME
solver image argument, and one optional string field:
`Certificate.status.acme.ari.certID`. No required field, served/storage API
version, RBAC rule, webhook rule, Service selector, resource bound, or persistent
storage reference changes. All CRDs retain served/storage version `v1`; the
live Certificate CRD's stored version is also `v1`. Existing `installCRDs: true`
renders these CRDs as chart templates, including the additive schema update.

All 50 rendered resources passed server-side dry-run against the current API.
The cert-manager regression tests protect ownership, CRD installation, chart
source, resource bounds, and CI wiring. Existing Longhorn and monitoring
admission safeguards were also tested.

At review time all three cert-manager Deployments were ready, all 24
Certificates were Ready, and `cluster-ca` was Ready. Dry-run is not a rollout
or issuance test: merging causes Flux/Helm to roll the controller, cainjector,
and webhook, and to run the chart's upgrade hook. With one replica per
component, transient issuance/admission unavailability remains possible;
existing application TLS Secrets are not deleted by this change.

After the coordinator merges, verify the HelmRelease is Ready at v1.21.3,
all three Deployments are available, `cluster-ca` and existing Certificates
remain Ready, and no new issuance/webhook errors appear. Do not delete
Certificates, Secrets, or CRDs as a rollback technique. A Git revert can roll
back the images; retaining the additive CRD field is preferable to removing
stored status data.

## Remaining PR #236 proposals

These are explicit dispositions, not claims that every deferred target is
incompatible. Separate chart PRs should receive their own render, migration,
and live-state review; no remaining change is authorized by this patch.

| Chart proposal | Evidence and disposition |
| --- | --- |
| Intel operator 0.36.0 to 0.37.1 | [Release support table](https://github.com/intel/intel-device-plugins-for-kubernetes/blob/v0.37.1/README.md#supported-kubernetes-versions) lists 0.37 against Kubernetes 1.37, but explicitly says the stable device-plugin API can work on older Kubernetes; CRDs are the exception. [0.37.0](https://github.com/intel/intel-device-plugins-for-kubernetes/releases/tag/v0.37.0) removes retired DLB/FPGA plugins and changes GPU capabilities/default memory; [0.37.1](https://github.com/intel/intel-device-plugins-for-kubernetes/releases/tag/v0.37.1) changes operator RBAC. **Defer to a GPU-specific PR** that proves existing GPU custom resources against the new CRDs, inventories other plugin CRs before removals, preserves `gpu.intel.com/i915`, and checks operator/plugin compatibility on 1.34. The GPU chart is already 0.37.1; blindly downgrading it is not part of this review. |
| NFD 0.19.0 to 0.20.1 | The [versioned README](https://github.com/kubernetes-sigs/node-feature-discovery/blob/v0.20.1/README.md#compatibility) explicitly supports Kubernetes 1.24+, so newer build libraries alone are **not** a compatibility block. [0.20.0 migration notes](https://github.com/kubernetes-sigs/node-feature-discovery/releases/tag/v0.20.0) change topology-updater RBAC from `get/update` to `patch`, creating mixed-version rollout/rollback hazards; they also change topology-updater rollout defaults. [0.20.1](https://github.com/kubernetes-sigs/node-feature-discovery/releases/tag/v0.20.1) adds CA distrust without public API changes. **Defer to a dedicated scheduling/GPU-label review** with exact old/new renders, topology-updater enablement/RBAC checks, and preserved node labels. Do not apply Descheduler's version rule to NFD. |
| Kyverno chart 3.8.2 to 3.9.2 | [Chart 3.9.2](https://github.com/kyverno/kyverno/blob/v1.19.2/charts/kyverno/Chart.yaml) installs Kyverno 1.19.2. The [support matrix](https://kyverno.io/docs/installation/releases/) covers Kubernetes 1.33–1.35, so 1.34 is compatible. Chart defaults now enable validating-admission-policy generation and reports and change permissions/dependencies. **Defer** until CRD conversion/migration, existing policies and admission behavior are reviewed separately. Preserve the preexisting Kyverno/policies suspensions; upgrading a chart must not silently resume enforcement. |
| Trivy chart 0.34.0 to 0.37.0 | Chart versions are not app versions: [chart 0.37.0](https://github.com/aquasecurity/trivy-operator/blob/v0.35.0/deploy/helm/Chart.yaml) installs operator 0.35.0, not nonexistent operator 0.37.0. [The operator release](https://github.com/aquasecurity/trivy-operator/releases/tag/v0.35.0) also changes the Trivy scanner to 0.75.0 and fixes content-based ConfigMap audit. **Defer** for intervening operator/scanner release review and exact report-CRD/schema, scan-resource/concurrency and metrics renders. Preserve existing concurrency 2 and disabled expensive assessment/compliance scans. SSD wear acceptance is not the reason for deferral. |
| Descheduler 0.36.0 to 0.37.0 | [Versioned compatibility guidance](https://github.com/kubernetes-sigs/descheduler/blob/v0.37.0/README.md#compatibility-matrix) states testing is limited to the three latest Kubernetes minors and associates 0.37 with 1.37; [go.mod](https://github.com/kubernetes-sigs/descheduler/blob/v0.37.0/go.mod) pins client libraries 0.37.1. Kubernetes 1.34 is outside that three-minor 1.35–1.37 window. **Reject this proposal on the present cluster**; retain 0.36.0 rather than introducing untested eviction/client skew. Constrain Renovate to `<0.37.0` while Kubernetes remains 1.34, and reassess the eviction policy/PDBs/capacity before any subsequent update. A successful Helm render cannot establish eviction safety. |
| KEDA 2.20.* to 2.21.* | [The tested matrix](https://keda.sh/docs/2.21/operate/cluster/) includes Kubernetes 1.34–1.36. [The migration guide](https://keda.sh/docs/2.21/migration/#upgrading-from-keda-220-to-221) requires explicit service-account token audiences, removes Temporal settings, and changes Azure Pipelines in-flight counting. **Defer** pending an inventory of ScaledObjects/ScaledJobs and TriggerAuthentication/ClusterTriggerAuthentication methods without reading Secret payloads, HTTP add-on compatibility, and CRD/admission renders. Prefer an exact reviewed chart patch to a new floating minor range. |
| Longhorn 1.12.1 to 1.13.0 | **Reject during recovery; preserve exact 1.12.1.** [The release](https://github.com/longhorn/longhorn/releases/tag/v1.13.0) requires Kubernetes 1.34+ (satisfied), introduces a default three-replica global-manager Deployment, and permits V2 live upgrade only from 1.12.2; 1.12.1 requires a separately planned detachment path if V2 is used. Backup/restore verification, engine inventory, replica health and schedulable maintenance capacity must gate a dedicated storage upgrade. Never alter the current 2Gi/2CPU manager bound, five-second readiness timeout, or two-replica safeguards to make a dependency PR pass. Disable automated Longhorn chart updates until recovery is explicitly complete. |
| Tailscale 1.98.9 to 1.104.2 | [Official changelog](https://tailscale.com/changelog) includes intermediate operator changes to ProxyGroups, peer relay, authentication recovery, RBAC and proxy behavior, not just a client patch. **Defer** to an operator-specific PR with the actual chart app version, CRD served/storage compatibility, proxy/Ingress inventory, OAuth Secret reference preservation, and rollback/render checks. Do not copy live credentials into validation values. |

## Bundle closure and automation

Once the replacement cert-manager PR is reviewed and the coordinator accepts
the dispositions above, PR #236 can be closed as superseded, not merged.
Remove the broad infrastructure grouping so the remaining compatible targets
can be proposed separately. Keep human review; do not enable blanket automerge.
Longhorn needs an explicit recovery hold and Descheduler a Kubernetes-dependent
ceiling. Other charts need per-product compatibility review rather than a
shared numeric Kubernetes/client-library ceiling.
