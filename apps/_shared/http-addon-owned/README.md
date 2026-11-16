# Owned HTTP add-on security derivative

These are not official KEDA releases. The exact stable 0.16.0 application source
is unchanged; the locked patch changes only `go.mod` and `go.sum`. Go 1.27.4,
KEDA 2.21.1, x/net 0.62.0 and their required module closure replace the older
compiler/dependencies embedded in all three current vendor images. The closure
also updates client-go/controller-runtime and removes the old Knative dependency;
this is not a compiler-only rebuild or a chart/core-operator-only fix.

The three amd64 factory images, original ELF hashes, patch/module hashes and
native fixture dependencies are immutable. Each output inherits the original
runtime layers, entrypoint, non-root user and execution settings. The only
existing filesystem replacement is its `/ko-app/` executable, preserving mode
0555 and ownership. Independent filesystem comparison rejects all other changes
or removals and permits additions only for corresponding-source/license/proof
material. Docker-generated hostname, hosts and resolver mounts are excluded.

Every actual ELF must retain symbols, report the declared compiler and linked
dependency versions, and match the embedded evidence. The combined three-root
source scan and each built-binary scan use the real Go advisory DB and reject
function or package-only matches. Module-only IDs and DB timestamps are retained,
not waived or described as universal clearance. Upstream short tests run without
production configuration; module licenses, the patch and all scan streams ship
under `/usr/share/http-addon-owned`.

GitHub-hosted CI creates two disposable, explicitly scoped Kubernetes 1.35
clusters: the exact factory baseline, then the actual local candidate images.
Both legacy HTTPScaledObject and direct InterceptorRoute/external-push paths
must proxy a real cold-start request and complete **0 -> 1 -> 0** without
fallback replicas, add-on restarts or increases to the production add-on limits.
Candidates are loaded directly into kind and use Never pull policy only inside
that fixture. No homelab kubeconfig, application data or credentials are used.
The test does not establish RKE2 or Kubernetes 1.36/1.37 compatibility.
The pinned official kubectl 1.36.6 is only this isolated test harness client;
it does not replace or clear the separately blocked production maintenance
helper. Core KEDA images and chart archives are pinned independently.

PRs do not publish. Only an explicit master dispatch can push the accepted local
artifacts without rebuilding. Every selected amd64 manifest must pull with an
empty registry credential configuration and retain the accepted execution,
filesystem and actual-ELF identity. A private or partially published package is
not accepted. New package visibility may require the owner to make it public.

Production activation is separate and requires fresh health/storage/recovery
evidence within the original outage budget. Chart 0.16.0 always appends one
shared tag to its three image names; it does not support invented per-image
digest keys. Use an exact Flux image post-renderer for immutable activation and
prove that unrelated rendered objects/fields remain unchanged. Preserve current
Paperless routing/scaling, the other ScaledObjects, resources and RBAC. No
application, HelmRelease, policy, host or node upgrade is activated by this build.
