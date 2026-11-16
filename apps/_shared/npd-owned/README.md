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
replacing only three programs and adding provenance. Debian libraries are not
claimed patched by this compiler change. Native CI compares vendor and owned
custom-plugin healthy/failure/recovery metrics, deliberately empty journal
library operation and invalid-regex failure, plus synthetic CRI success/failure.
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
against the exact public source, including after publication. Keep
the controller cordoned and Kured suspended; this is not node maintenance.
