# Held same-version Prometheus security candidate

This is a candidate-only recipe, not an activated image or a publication
workflow. Prefer an accepted official stable patch when one becomes available.
The currently assessed vendor is the **distroless** 3.16.0 image in `source.json`.
Do not silently replace it with BusyBox or rebuild its UI from newer npm inputs.

The source commit and official UI archive are locked. The only tracked upstream
changes are the reviewed `go.mod`/`go.sum` patch. The three direct security pins
are x/crypto, x/net and x/text; x/crypto and x/text require the declared x/sys,
x/term and x/sync minimum-version closure. Build the release root module with
`GOWORK=off`, excluding ancillary compliance/documentation/UI-generator modules.
Actual linked dependency comparisons must reject changes outside those six
pairs. Both ELF programs retain symbol tables and use the patched compiler.
Source and binary advisory checks reject function and package-wide findings;
module-only matches and authoritative database timestamps remain in evidence.
Prefetch only the selected real upstream test packages with `go list -deps -test`
and verify the frozen module hashes immediately afterward. A blanket
`go mod download all` adds unrelated checksum entries and is not accepted.

The final stage inherits the exact vendor filesystem and execution identity,
including its non-root user, entrypoint, working directory and CA stores.
Only the two executables and licensed provenance may differ. The official
compressed UI assets are embedded using the pinned upstream compression script.
The vendor's literal configured user is `65532`; its group resolution remains
in the unchanged vendor filesystem. Bind the pulled image's repository digest,
saved raw OCI config digest, execution config and rootfs together; Docker engine
image IDs may identify either the config or manifest. Retain the candidate's
actual archive config digest separately for scanner identity checks.
Native acceptance uses disposable, network-isolated hosted CI, synthetic metrics
and data only: TLS positive/negative cases, real UI/API/config reloads, and a
vendor -> candidate -> vendor TSDB/WAL write/read/reopen sequence. These are
bounded compatibility checks, not a production recovery rehearsal.

No automatic publication or cluster activation is permitted. Preserve the
accepted actual image archive and bind a canonical all-severity/full-inventory
scan to it before promotion. Source/native success is not whole-image security,
default-root PKI, production compatibility, or forensic clearance. A future
activation also needs its own valid recovery and outage gates; the expired
previous maintenance window cannot be reset to deploy this candidate.
