# Owned supported-minor node detector

This locks the official NPD **1.35.4** source/runtime for the current 1.35 API,
not the newest 1.37 line. Upstream explicitly aligns support and testing to the
Kubernetes minor. Production's older detector is not changed by this recipe.

All three actual official programs were independently exported and found to
use Go 1.27.2, with actual-binary matches for GO-2026-6809/CVE-2026-111943
(`crypto/x509`) and GO-2026-6859/CVE-2026-97054 (`net/http`). Presence does not
establish exploitability in the deployed configuration. Rebuild all three with
Go 1.27.4 using the unchanged upstream CGO/journald make targets; do not patch
only PID 1, remove journald, strip symbols or edit dependency versions.

The exact commit, official tag, original three ELF hashes and unchanged module
hashes are locked. Upstream short race tests run with journald. Source and
actual-binary govulncheck scans reject every function/wildcard finding and retain
module-only records and DB timestamps. Actual linked module/Go licenses and
raw scans accompany the source/ELF proof. No source or module mutation is allowed.

The runtime inherits the exact supported-minor vendor filesystem/configuration,
replacing three programs, applying two exact Debian APT security packages and
adding provenance. Official package archives are SHA-256 locked, downloaded
over HTTPS, and installed by `dpkg` with networking disabled: vulnerable runtime
APT is never used to fetch its own patch. Build-only APT fetches explicitly
disable retries, avoiding the vulnerable bootstrap retry behavior without
disabling signature checks. The 83-package set must remain identical, both
declared upgrades must reach 2.6.2, and all other 81 complete package metadata
sections must remain unchanged. Actual package-status hashes are rechecked
after immutable publication. This does not claim all Debian libraries patched.
Native CI compares vendor and owned
custom-plugin healthy/failure/recovery metrics, deliberately empty journal
library operation and invalid-lookback failure, plus synthetic CRI success/failure.
Upstream only evaluates regexes while processing records; an empty journal
cannot establish malformed-regex rejection. The negative fixture instead uses
an eagerly validated duration and requires the actual parse error/Unknown exit.
Everything runs network-none, read-only and capability-free with repair disabled;
the actual current 100m CPU/128Mi memory limits are exercised without raising
production resources, and the detector must remain running without OOM.
No real node, host PID/device/journal/socket, kubeconfig, API or private data.
Empty-journal ABI acceptance is not evidence of real journal ingestion or API
condition reporting, and public TLS is not exercised.

No PR publishes. Explicit master publication pushes the accepted artifact once,
then verifies immutable platform selection, complete Config/RootFS/platform/time
identity and **all three** actual ELF/scan proofs after pull. Deployment requires
a separate immutable image activation, current-minor check, unchanged resource/
RBAC settings, natural rollout and actual three-node observation. Upstream moved
`ReadonlyFilesystem` detection out of `kernel-monitor.json` into the separate
`readonly-monitor.json`; activation must explicitly retain the current kernel
and systemd monitors **and add this readonly monitor**, or existing filesystem
alerts would silently lose coverage. All three actual config files are hashed
against the exact public source, including after publication.

Upstream also changes the effective API rate ceiling: production v0.8.19 uses
client-go's default 5 QPS/10 burst, while v1.35.4 defaults to 500/500. Activation
must explicitly set `--kube-api-qps=5 --kube-api-burst=10`, preserving the existing
5-minute heartbeat rather than silently increasing pressure on this control
plane. The disposable test accepts these exact flags with API export disabled;
it does not claim to measure real API request rates. The active chart 2.3.14
supports `image.digest` and `settings.extraArgs`; pin that exact chart on
activation rather than leaving the existing wildcard version.

The reviewed Debian factory still has OS package findings. This producer fixes
APT advisory CVE-2026-109518 (`apt` and `libapt-pkg6.0` 2.6.1 -> 2.6.2), not all
remaining OS records. Activation requires a separate fresh immutable-image scan
and component-level assessment. Retain all raw severities, including UNKNOWN,
and distinguish source-package records from actual component applicability
rather than silently excluding them. In particular, Debian notes that the
MiniZip code in CVE-2023-45853 was not built by bookworm's zlib source; that
primary qualification is not a blanket waiver or permission to erase records.

Keep the controller cordoned and Kured suspended; this is not node maintenance.
