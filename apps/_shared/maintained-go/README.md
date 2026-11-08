# Maintained same-version Go derivatives

These are user-owned compiler rebuilds, not official vendor releases. No
HelmRelease is changed here. Deployment remains a separate serial operation
behind fresh storage/backup gates; retain all existing read-only CA guards.

`sources.json` locks public source, original amd64 runtime, app version and the
Go 1.27.3 musl builder. The runtime inherits every vendor layer, configuration,
entrypoint, user and healthcheck. Only the application binary is replaced;
license and corresponding-source/build evidence are added under
`/usr/share/maintained-go`. Original upstream repositories and exact revisions
provide corresponding source, with the compiler recipe maintained here.

Gluetun uses its original no-CGO, trimpath and version/date/commit linker
flags. Stash uses the upstream static-PIE release make targets and SQLite,
osusergo/netgo tags, the exact released UI archive (SHA-256 checked), generated
backend/login assets, and original version linker values. Stash's historical
`officialBuild` feature flag is retained for behavior compatibility; this
derivative is **not** an official Stash distribution.

The locked gqlgen version cannot load Go 1.27 export data. Its generated
source is prepared separately with pinned Go 1.26.10; both stages reject
changes to go.mod/go.sum. The shipped application is compiled with Go 1.27.3,
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

The Hardcover rreading-glasses factory provenance identifies revision
`97dd4a676d435abc400fac93f303a725cde6b0d9`, unavailable from public upstream
Git, commit API and codeload. The factory-matched rebuild remains unavailable;
that source has not been recovered or silently replaced. A separately approved
replacement-source line is locked in `../rreading-owned/source.json`. Its
public GPL fork preserves the selected signed PR587 base, adds the queue-owned
pending metric fix tracked in upstream issue612, and patches advisory-affected
dependencies. The separate `rreading-owned-build.yaml` publisher requires
the actual clean Go 1.27.3 image to pass original race regressions, native
TLS/API/restored-cache tests, and source plus binary advisory gates before
master-only publication. Production activation remains separate, with real-cache
and cold-factory rollback compatibility, fresh durable backup and unchanged
two-worker storage gates. PostgreSQL and its original claim are not migrated.

This proves a patched compiler and bounded native behavior, not universal
custom/intermediate TLS validation, a vulnerability-free dependency tree,
media compatibility or forensic clearance.
