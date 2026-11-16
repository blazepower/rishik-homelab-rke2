# Maintained same-version Go derivatives

These are user-owned rebuilds, not official vendor releases. No
HelmRelease is changed here. Deployment remains a separate serial operation
behind fresh storage/backup gates; retain all existing read-only CA guards.

`sources.json` locks public source, original amd64 runtime, app version and the
Go 1.27.4 musl builder. The runtime inherits every vendor layer, configuration,
entrypoint, user and healthcheck. Stash replaces only the application binary.
Gluetun additionally has the explicit security overlay described below;
license and corresponding-source/build evidence are added under
`/usr/share/maintained-go`. Original upstream repositories and exact revisions
provide corresponding source, with the compiler recipe maintained here.

## Declared Gluetun security overlay

The Go compiler update alone did not patch older separately linked modules or
Alpine packages. Gluetun now applies the SHA-256-locked `gluetun-security.patch`
to **only** `go.mod` and `go.sum` at the original source revision. It updates
circl, compress, x/crypto, x/net and x/text plus their required module graph.
Exact post-patch module-file hashes reject drift; application source, version,
VPN settings and original linker values remain unchanged. The manifest declares
these changes separately: this Gluetun build is no longer compiler-only.

The inherited Alpine 3.22 runtime also upgrades **only** `pcre2=10.46-r1`.
Native acceptance compares the actual before/after APK inventories and rejects
any undeclared package, version or architecture change. Inherited vendor layers
remain intact, but this explicitly permitted package overlay means whole-rootfs
identity is not claimed. PCRE2's Alpine backport addresses CVE-2026-89157 and
CVE-2026-89161; the former's 32-bit applicability must not be generalized to
the other advisory.

The actual patched source passes upstream short tests and govulncheck v1.9.0
source **and actual built-binary** scans. JSON mode can return exit zero with
findings, so acceptance parses every finding and rejects function or
package-wide matches, including wildcard function entries. Module-only matches
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
