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
Git, commit API and codeload. It is blocked, not silently replaced by main or
a newer application. Its separate preserved PostgreSQL upgrade is out of
scope.

This proves a patched compiler and bounded native behavior, not universal
custom/intermediate TLS validation, a vulnerability-free dependency tree,
media compatibility or forensic clearance.
