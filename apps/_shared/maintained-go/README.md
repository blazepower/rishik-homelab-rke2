# Maintained same-version Go derivatives

These are user-owned rebuilds, not official vendor releases. No
HelmRelease is changed here. Deployment remains a separate serial operation
behind fresh storage/backup gates; retain all existing read-only CA guards.

For Gluetun and Stash, `sources.json` locks public source, original amd64 runtime, app version and the
Go 1.27.4 musl builder. The runtime inherits every vendor layer, configuration,
entrypoint, user and healthcheck. Stash replaces only the application binary.
Gluetun additionally has the explicit security overlay described below;
license and corresponding-source/build evidence are added under
`/usr/share/maintained-go`. Original upstream repositories and exact revisions
provide corresponding source, with the compiler recipe maintained here.

## Owned minimal kubectl client

`kubectl.json` separately locks Kubernetes v1.36.6's exact public source,
Go 1.27.4 and a dependency-only `go.mod`/`go.sum` patch, without changing the
two application recipes. This client minor can span API servers 1.35 through
1.37 under Kubernetes' one-minor kubectl skew policy; it is not a server upgrade.
The shallow exact-revision fetch avoids downloading Kubernetes' full history.
The official release tag is fetched separately and must resolve to that exact
revision; the actual ELF must record `v1.36.6+dirty` as its main module version.
Missing shallow-clone release metadata must not turn old, fixed version ranges
into apparent current findings.
`GOWORK=off` and readonly module mode are required: the monorepo's original
workspace/vendor graph must not silently override the reviewed patch.

The linked ELF must contain OTEL root/trace 1.42.0, x/net 0.62.0 and
x/text 0.44.0. Other required graph updates are bound by both module-file hashes,
not falsely asserted to be linked. The same source/binary symbol/wildcard gates
apply. The two coarse binary-only records
[GO-2025-3521](https://vuln.go.dev/ID/GO-2025-3521.json) and
[GO-2025-3547](https://vuln.go.dev/ID/GO-2025-3547.json) are retained, not suppressed.
The former concerns kubelet gitRepo volumes, absent from the CLI import graph;
the latter concerns network-policy removal during namespace termination.
Neither record supplies affected functions/packages. Compiler changes to a CLI
do not remediate those cluster behaviors. Any change to the exact reviewed
module-only advisory sets stops acceptance.

**Blocked, not accepted:** the first Linux build failed the actual-binary
advisory gate. Independent unstripped reproduction also expands the two
whole-monorepo records above to `cmd/kubectl.main`. The earlier stripped
cross-compile's module-only result was not proof of complete symbol analysis.
The exact patched Linux import closure has 907 packages and its only
`k8s.io/kubernetes` package is `cmd/kubectl`, but that assessment does not waive
the binary gate. No symbol stripping, renamed module, alternate database,
`main` exception or claim of zero binary findings is permitted to make this
recipe pass. Release metadata correction is not remediation of these records.
Native behavior, licensing, publication and deployment remain unaccepted until
the remaining gate is resolved.

The November 22 recheck retained the exact tagged source revision
`1b2171b110bb2a2542e1322035c6583ec86eb976`, post-patch module hashes and
unstripped Go 1.27.4 Linux/amd64 build flags. Windows-hosted cross-compilation
succeeded; the four upstream get/exec/rollout/version short-test packages passed
on Windows. The live govulncheck v1.9.0 database timestamp was
`2026-11-11T02:51:11Z`: source analysis returned no findings, but binary analysis
returned module, package and `main` findings for **both** records. The current
master's stricter package-only rejection is preserved alongside the CLI-specific
regression, not weakened to accept this artifact. These host checks do not
substitute for Linux image/native-runtime acceptance.

Neither current Go advisory record has a `fixed` version event or an affected
package/function list. The latest available stable 1.36 source is already
v1.36.6; 1.37 is outside supported skew for a 1.35 API server. Choosing another
root-module release or updating unrelated CLI dependencies cannot leave these
open-ended module ranges. The upstream
[namespace-termination advisory](https://github.com/kubernetes/kubernetes/issues/126587)
also states that no fixed version exists. A genuine accepted source/advisory
resolution is still required; cluster-side mitigations, source-only clearance,
renaming the main module or changing the scanner are not substitutes.

Unlike the large `alpine/k8s` reference image, the owned runtime uses the pinned
official Alpine 3.24.4 amd64 base and adds only kubectl and its source/licenses.
The base's 16-package inventory is retained except the explicit, signed
`zlib=1.4-r0` upgrade for CVE-2026-4738, available in the original stable
[package index](https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/APKINDEX.tar.gz)
and [security database](https://secdb.alpinelinux.org/v3.24/main.json).
No auxiliary cloud/Kubernetes CLIs from the reference image are shipped.
Complete package/version/architecture equality rejects undeclared mutations.
The derivative retains Kubernetes' license, Go's license and licenses/notices
for every actual linked module, not only the upgraded modules.

Native CI independently hash-checks the official reference kubectl against the
primary release checksum. Both reference and derivative must perform actual
get/list, WebSocket exec output, nonzero exec error and rollout-restart PATCH
operations against a disposable host-loopback synthetic API. No real kubeconfig,
service-account token, cluster, private media or records are used. The derivative
reports `v1.36.6+homelab.go1.27.4` and a dirty tree, never an official-build claim.
Its actual public-CA bundle must exclude every currently blocked SPKI.
Normal publication re-pulls and re-proves the exact ELF and complete runtime
identity. Building/publishing does not change the mount-handler CronJob: its
image/Renovate/provenance activation and natural scheduled acceptance are separate.
No whole-image, auxiliary-reference-image or forensic clearance is implied.

## Declared Gluetun security overlay

The Go compiler update alone did not patch older separately linked modules or
Alpine packages. Gluetun now applies the SHA-256-locked `gluetun-security.patch`
to **only** `go.mod` and `go.sum` at the original source revision. It updates
circl, compress, x/crypto, x/net and x/text plus their required module graph.
Exact post-patch module-file hashes reject drift; application source, version,
VPN settings and original linker values remain unchanged. The manifest declares
these changes separately: this Gluetun build is no longer compiler-only.

The inherited Alpine 3.22 runtime upgrades **only** the six packages locked in
`security.runtime_packages`: `pcre2=10.46-r1`, `libcrypto3=3.5.9-r0`,
`libssl3=3.5.9-r0`, `musl=1.2.5-r13`, `musl-utils=1.2.5-r13` and `zlib=1.4-r0`.
These versions are available in the original stable branch's signed amd64
package index; no edge repository, floating upgrade or distribution migration
is used. The ABI-coupled OpenSSL libraries are pinned together. Native acceptance
compares the complete actual before/after APK inventories and rejects any
undeclared package, version or architecture change. Inherited vendor layers
remain intact, but the explicit package overlay is not whole-rootfs identity.

The primary [Alpine 3.22 security database](https://secdb.alpinelinux.org/v3.22/main.json)
records the OpenSSL fixes at 3.5.8-r0, musl CVE-2026-107891 at 1.2.5-r13,
zlib CVE-2026-85091 at 1.3.2-r1 and CVE-2026-4738 at 1.4-r0.
The scanner reported these library findings as **UNKNOWN**, some with scores
up to 8.1: zero HIGH/CRITICAL counters were not evidence of a patched image.
PCRE2's backport addresses CVE-2026-89157 and CVE-2026-89161; the former's
32-bit applicability must not be generalized to the other advisory.

The actual patched source passes upstream short tests and govulncheck v1.9.0
source **and actual built-binary** scans. JSON mode can return exit zero with
findings, so acceptance parses every finding and rejects function or
package-wide matches, including wildcard function entries and package-only
traces with no function field. A package-only result can reflect conservative
analysis rather than a proven reachable function; it still blocks acceptance.
Module-only matches
and the real advisory DB timestamp remain in provenance rather than being
silently treated as clearance. The image retains the exact patch, updated
module licenses and corresponding-source/build evidence. An independent
runtime-ELF check verifies the declared dependency versions and scan/hash binding,
including after immutable publication. This does not prove hostile-input runtime
reachability, full-image vulnerability coverage or a working production VPN.

## Compiler and native acceptance

Gluetun uses its original no-CGO, trimpath and version/date/commit linker
flags. Stash uses the upstream static-PIE release make targets and SQLite,
osusergo/netgo tags, the exact released UI archive (SHA-256 checked), generated
backend/login assets, and original version linker values. Stash's historical
`officialBuild` feature flag is retained for behavior compatibility; this
derivative is **not** an official Stash distribution.

The locked gqlgen version cannot load Go 1.27 export data. Its generated
source is prepared separately with pinned Go 1.26.11. Its module files stay
unchanged in both stages; Gluetun permits only the hash-locked patch above.
The shipped application is compiled with Go 1.27.4,
and acceptance checks that actual runtime ELF, not the generator toolchain.

Hosted Linux CI copies the actual rebuilt ELF out of the runtime image,
checks its compiler, flags, source binding and hash, and exercises it rather
than a helper executable. Stash serves locked native UI assets and writes,
reads and reopens a disposable synthetic SQLite database on a network-none
container. Gluetun's native startup rejects the missing default route and
reports its native unhealthy state without keys, NET_ADMIN or privileged
network workarounds. That is not acceptance of a real VPN connection.

PRs never publish. After exact-head PR checks and merge, explicitly dispatch
`maintained-go-build.yaml` on master with `publish=true`. Each accepted local
artifact is pushed without rebuilding, pulled by immutable digest and its
actual ELF checked again. Evidence artifacts include immutable amd64 image
values, source revisions, compiler output and bounded behavior results.
An OCI index digest is recorded separately from its unique linux/amd64 runtime
manifest. Platform selection excludes attestation descriptors and rejects
missing or ambiguous runtimes; the selected manifest is pulled and its actual
ELF must still match the accepted local artifact before it is recorded.
Docker-local image IDs are recorded for diagnostics, not used as portable
runtime identity across an index/platform pull. Acceptance instead requires the
complete execution configuration, ordered filesystem layer hashes, platform and
creation metadata to match the accepted local image exactly, then repeats the
actual ELF/provenance checks. Evidence is retained even when publication fails;
its explicit stage distinguishes an uploaded-but-unaccepted image from an
accepted immutable publication. A successful push alone never authorizes rollout.

The Hardcover rreading-glasses factory provenance identifies revision
`97dd4a676d435abc400fac93f303a725cde6b0d9`, unavailable from public upstream
Git, commit API and codeload. The factory-matched rebuild remains unavailable;
that source has not been recovered or silently replaced. A separately approved
replacement-source line is locked in `../rreading-owned/source.json`. Its
public GPL fork preserves the selected signed PR587 base, adds the queue-owned
pending metric fix tracked in upstream issue612, and patches advisory-affected
dependencies. The separate `rreading-owned-build.yaml` publisher requires
the actual clean Go 1.27.4 image to pass original race regressions, native
TLS/API/restored-cache tests, and source plus binary advisory gates before
master-only publication. Production activation remains separate, with real-cache
and cold-factory rollback compatibility, fresh durable backup and unchanged
two-worker storage gates. PostgreSQL and its original claim are not migrated.

This proves a patched compiler and bounded native behavior, not universal
custom/intermediate TLS validation, a vulnerability-free dependency tree,
media compatibility or forensic clearance.
