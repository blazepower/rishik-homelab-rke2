# Standalone scanner cache

Each standalone Job scans the workload's regular and init-container images in
parallel. Its scanners share the downloaded, read-only vulnerability database.
They must not also share Trivy's exclusive BoltDB filesystem scan cache: that
causes `cache may be in use by another process` failures and missing reports.

The chart's supported custom-volume settings mount `scan-cache-config.yaml`
read-only at `/trivy.yaml`. The pinned upstream scanner starts in `/` and loads
`trivy.yaml` by default. `cache.backend: memory` gives each process an independent
scan cache while retaining the shared vulnerability database. It does not disable
vulnerability/secret scanning or registry TLS verification, add privileges,
introduce Redis, or modify application Pods.

Operator 0.36.1 reads the custom-volume configuration at startup. The chart's own
`checksum/config` does not track it. `podAnnotations.checksum/scan-cache` therefore
binds the volumes, mounts and public cache configuration to the operator Pod
template so Flux/Helm rolls the operator when they change. The acceptance script
recomputes that checksum from the actual rendered settings; update it alongside
those settings. Merely updating the ConfigMaps does not activate the mounts in an
already-running operator.

Keep the two-Job limit, one-hour diagnostic retention, worker2 placement and
per-container resource bounds. Memory cache discards per-process analysis after
the scan; subsequent scans repeat that analysis. The Job limit is not a scanner
process limit: a multi-container workload starts multiple bounded processes.
Also, operator 0.36.1 counts retained completed Jobs against its concurrency
limit, so fresh coverage can still lag. Missing or unmatched reports are not zero
findings.

`python3 -B .github/scripts/test-trivy-scan-cache.py` renders the exact pinned
chart and uses its actual scanner image, volume wiring and rollout checksum. On a Linux Docker
host it first proves an exclusive filesystem-cache lock causes failure, then
proves the mounted configuration allows an offline, non-root, network-isolated
fixture scan while the same lock is held. `--render-only` checks chart wiring
without claiming native acceptance. No cluster scan or private data is used.

Scanner 0.76.2 includes the upstream base-image, x/net and go-jose security fixes;
that is not universal vulnerability clearance.

References:

- [Upstream cache-lock guidance](https://trivy.dev/docs/v0.76/guide/references/troubleshooting/#cache-lock-errors)
- [Scanner 0.76.2 release](https://github.com/aquasecurity/trivy/releases/tag/v0.76.2)
