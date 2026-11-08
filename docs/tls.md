# TLS Certificate Management

## Application runtime trust incident coverage

The scoped October 26, 2026 application evidence is recorded in
`.github/security/fleet-application-ca-report.json`. Its status is **blocked**:
PEM-only clearance, a patched runtime version, and a clean Node default store
must not be mistaken for complete application trust clearance. The report
distinguishes image indexes from exact amd64 manifests, Calibre-Web's denied
packaged certifi from its clean OS bundle, and Seerr's clean default Node roots
from its denied system roots. It contains no deployment authorization; the
coordinator's failed storage baseline remains a separate rollout gate.

Run `python .github/scripts/test-fleet-application-ca.py` to validate the evidence
and non-clearance invariants. Existing separately verified Plex runtime
hardening and unchanged owned application images are outside this report.

### Calibre-Web certifi overlay preparation

`apps/calibre-web/harden-certifi.py` prepares a dedicated ephemeral copy of the
exact pinned image's public certifi package, removing only the eleven-policy
SPKIs and preserving every other certificate and the original Python modules.
The entire package is mounted read-only, rather than redirecting only Requests
or replacing just one writable file. The existing library bootstrap remains
first; a failed hardening initializer prevents the unmodified vendor `/init`
from starting. The guard has no mounts of user configuration or library PVCs.
No custom init hook, runtime package installation, or user trust-store rewrite
is introduced. A vendor image refresh must revalidate and update the package
fingerprints; an unknown package intentionally blocks startup.

Preparation evidence and subsequent isolated acceptance are in
`.github/security/fleet-application-runtime-ca-hardening-report.json`.
Run 40943923000 at `fc33b2d` passed all thirteen Calibre checks without skips,
including the strict filesystem barriers and selected app-template 5.3.0.
This is **not deployed or live-cleared**; the storage and serial activation
gates still apply. Run
`python .github/scripts/test-application-runtime-ca-hardening.py` on a Linux
Docker runner, together with
`python .github/scripts/test-calibre-library-bootstrap.py`. Docker executions
use the exact pinned image with networking disabled and no user configuration
or media. Windows-only passes with Docker tests skipped are not clearance.

### Additional held preparations

Sure has a same-image Ruby initializer for both Rails and Sidekiq, with
read-only default/OS, AWS and Skylight public trust overlays. It preserves
the original commands and requires exact-image Linux startup with disposable
loopback-only PostgreSQL/Redis before acceptance. No real database or
credentials are used. See `test-sure-runtime-ca-hardening.py` and its scoped
preparation report. The corrected Bundler-aware Sidekiq registration probe
and full Linux suite passed at `93c0ffa` (14 tests, zero skips,
[job 123719104946](https://github.com/blazepower/rishik-homelab-rke2/actions/runs/40951095423/job/123719104946)).
This is isolated acceptance, not deployment clearance.

Bookshelf, qBittorrent/Gluetun and Seerr system stores reuse the shared media
guard. Their separate Linux gate checks original isolated startup and
read-only public stores; Seerr also checks native default/bundled/system root
inventories. This is not .NET/Qt/custom TLS or credentialed VPN clearance.
The registry/Docker/render suite passed at `93c0ffa` (16 tests, zero skips,
[job 123719104843](https://github.com/blazepower/rishik-homelab-rke2/actions/runs/40951095423/job/123719104843)).
Gluetun's public-store barrier passed, but its Go 1.25.11 remains below the
incident floor; no real VPN startup was attempted.

The separate `Dotnet Public Store CA Acceptance` gate probes Bookshelf's
unchanged self-contained .NET 6.0.36 CLR, native apphost, runtime configuration
and dependency files. Only the managed main assembly is replaced in a disposable
fixture; nothing is rebuilt, published or deployed as a vendor application.
The pinned .NET 6.0.428 SDK is an offline-only compiler with no NuGet sources,
credentials or network access, not an accepted production runtime. Real denied
public roots must be trusted before filtering and rejected specifically for
root trust afterward, while ten retained roots remain trusted. Read-only
overlays use the production filter. This is standard `X509Chain` system-anchor
evidence at a fixed policy-test time, not TLS handshakes, intermediate-key
denial, actual daemon/custom contexts, user-store inventory or .NET 6 support
and comprehensive security clearance.

The separate `Qt Public Store CA Acceptance` gate uses the unchanged fully
static qBittorrent executable, not an external Qt helper. Its real HTTPS
URL-downloader, RSS reader and independent libtorrent HTTPS tracker stack contact
two loopback-only generated CA/server chains in a network-disabled disposable
profile. Both work before filtering; afterward the retained control must still
work and the excluded chain must produce a native trust diagnostic without an
HTTP request. RSS acceptance requires parsing the empty synthetic feed, not
just fetching an icon. Tracker acceptance requires native working/error states;
negative acceptance combines libtorrent's certificate-verification failure with
the server's unknown-CA alert. Transport closure, timeouts and unrelated SSL
errors cannot pass. The original CLI first applies
its unchanged exactly-eleven-key policy. A separate test-API pass then adds one
explicitly synthetic denied key while retaining all eleven real keys; the
production policy loader and shared guard are never modified.
It is a store-selection/actual-daemon-context test, not real-key signing or
eleven-key native/intermediate rejection proof. Empty feeds disable automatic
downloads; synthetic magnets receive zero peers and cannot obtain metadata or
payload. DHT/LSD/PeX remain disabled. SSRF mitigation is disabled only for the
unpublished loopback tracker fixture; certificate verification remains enabled
and actual preferences are checked. No live profiles, live trackers, peer/payload
TLS, search/plugins, other custom contexts, VPN or production authentication are
cleared. Localhost authentication bypass exists only in the unpublished,
loopback-bound, network-disabled disposable profile; no real config is used.

The retired Kindle Sender has a pinned Go 1.27.3 rebuild and non-publishing
Linux acceptance workflow. Native tests use the exact eight public SPKIs and
require certificate-policy rejection, not an arbitrary handshake failure.
The maintained static-CGO/SQLite image must pass before publication and a
separately reviewed immutable release pin if reactivation is ever authorized.
The active kustomization retains only the historical data PVC; this preparation
does not restore the sender runtime, publish its image or reactivate it.
The isolated acceptance passed again at `93c0ffa` in
[run 40951095407](https://github.com/blazepower/rishik-homelab-rke2/actions/runs/40951095407),
with the same accepted binary and public bundle hashes as `7ee1cf9`.
Explicit custom TLS contexts and other compiled Go gaps remain outside
these overlays. Historical audits remain timestamped pre-deployment baselines.

On October 29, RKE2 published
[`v1.34.13-rc1+rke2r1`](https://github.com/rancher/rke2/releases/tag/v1.34.13-rc1%2Brke2r1)
with release notes declaring Go 1.26.10. The checked official v1.34 stable
channel still resolves to `v1.34.12+rke2r1`. Read-only ELF inspection of the
checksum-verified public amd64 RKE2 server binary confirms
`go1.26.10-X:boringcrypto`. This does not verify kube-apiserver, kubelet,
containerd, native CA behavior or any deployed node. The RC is not an accepted
stable upgrade; no node changes or prerelease deployment are authorized.

### Controlled activation

Radarr is the first separately authorized serial activation, using the already
audited same-application-version vendor rebuild. Verify its actual running image,
public trust stores, authentication and storage before activating another release.

Calibre is separately authorized after storage-owner clearance as CWA v4.0.8.
Its migration preserves the existing library/accounts and OPDS URLs. The pinned
CWA certifi, pip-certifi, native OpenSSL bundle/hashed-directory overlays and
vendor startup are audited in `apps/calibre-web/harden-certifi.py`; isolated
native ingestion and unchanged OPDS bytes were verified, and CI 41180623130
passed. The historical stock-image report is not CWA live-clearance evidence.
Live activation must still verify running trust stores, accounts, library hashes,
and a new NAS-to-OPDS import. See the Calibre acquisition-bridge procedure.

Bazarr, Prowlarr, SABnzbd, Bookshelf, Seerr, qBittorrent/Gluetun and Sure are
enabled through separate serial activations of their accepted public-CA startup
guards. Each activation requires storage-owner clearance, a fresh safety check,
exact-head CI, and verification of the running immutable image, completed guard,
actual read-only public trust overlays, startup, authentication behavior and
data mounts before proceeding. Sure applies to both Rails and Sidekiq; verify
the current Sidekiq process registration and fresh heartbeat, not just HTTP.
Public-store filtering does not certify every .NET, Qt, compiled-Go or custom
TLS context. Preparation reports and passing CI alone do not prove live safety.

The historical November 5 refresh found the supported RKE2 v1.34 channel at
`v1.34.12+rke2r1` and `v1.34.13+rke2r1` marked as a prerelease at that time.
The vendor-image ELF inspection found Gluetun v3 on Go 1.25.11,
Stash v0.32.0 on Go 1.25.9 and rreading-glasses hardcover on Go 1.26.0, below
the incident floors. Official `syncthing/syncthing:2.1.7` is a same-vendor
candidate with Go 1.27.3 and clear public PEM stores, not yet an accepted or
deployed migration at that refresh. Compiler versions and PEM inventories are not native
or custom-context chain-rejection certification.

The November 6 release recheck now marks same-minor
[`v1.34.13+rke2r1`](https://github.com/rancher/rke2/releases/tag/v1.34.13%2Brke2r1)
stable. Publisher checksums and the actual server ELF, plus selected runtime,
Kubernetes and etcd image ELFs, were independently checked: all inspected
compilers are Go 1.26.10. This is selected-artifact metadata, not every component
or universal native/custom TLS certification.

The operator completed the stable same-minor upgrade on November 6, serially:
controller, worker2, then worker1. All three nodes are Ready on
`v1.34.13+rke2r1`, with native RKE2 reporting `go1.26.10-X:boringcrypto`
and containerd reporting `2.2.10-k3s1`. Protected completion receipts were
independently bound to each current service PID/invocation and the
publisher-verified RKE2 executable. Installed selected runtime hashes match
the inspected artifacts; worker receipts also verify the running kubelet and
containerd executables. Running control-plane image indexes were bound to the
inspected amd64 manifests, the actual API-server executable matches its inspected
ELF, and the API and running etcd executable report Go 1.26.10.

Before the sole-control-plane restart, a fresh local etcd snapshot passed
appended-checksum and offline native database-status verification. This was not
a real-data restore test; retain that snapshot and the old node binaries.
The unchanged three-sample storage gate passed after the final node:
27 attached volumes with two RW replicas and seven detached volumes with two
clean worker copies, with legacy quarantine unchanged. Post-upgrade acceptance
also confirmed cluster DNS, Plex identity/NFS mounts, hostname-and-chain-verified
Plex/Stash HTTPS through ingress, and native VPN health/forwarding-file presence.
That final forwarding-file check is not a new external-port-openness test.

The controller remains cordoned and Kured suspended. No drain, forced stop,
OS upgrade or reboot was performed. Existing workload containers and historical
container shims were not deliberately restarted; installing the patched shim
does not certify every surviving old process or auxiliary component. Do not
bypass sudo or deploy a prerelease/minor jump. Before any future sole-control-plane
restart, create and verify a fresh recovery snapshot again.

The separate `Syncthing Upgrade Acceptance` workflow rehearses official 1.28.1
to 2.1.7 using disposable generated identities, databases and book fixtures on
an internal Docker network. It keeps the vendor entrypoint, UID/GID 1000,
configuration/media paths, health endpoint and 500m/512Mi limits, and checks
SQLite migration, GUI/API authentication and identity preservation, bidirectional
1.x peer synchronization, cold restart and rollback from a stopped 1.x database
snapshot. The candidate omits the obsolete default-folder option and explicitly
retains deleted-file metadata indefinitely with
`--db-delete-retention-interval=0`. Only synthetic data may enter this rehearsal.
It does not deploy or authorize a production major upgrade: that still needs a
fresh verified backup, stable storage, migration capacity/time assessment and
explicit acceptance of the v2 deleted-file conflict semantics.

The approved production transition first stages the pinned official candidate
with zero replicas. This quiesces the old database before creating and verifying
a retained Longhorn snapshot/backup of the existing config PVC. Only a separate
activation change restores one replica after backup and storage acceptance.
The existing identity, GUI authentication, books PVC and indefinite deleted-file
retention must remain intact. Rollback restores the stopped legacy backup and
old image together; the old executable must never be pointed at the migrated
SQLite database. The separate immutable v2 runtime record preserves the original
factory audit and requires its already-clean public stores to remain clean.

This approved transition was deployed in
[PR416](https://github.com/blazepower/rishik-homelab-rke2/pull/416) and
[PR417](https://github.com/blazepower/rishik-homelab-rke2/pull/417).
Live acceptance verified native 2.1.7/Go 1.27.3, two SQLite databases, unchanged
identity certificate and private-file stat metadata, retained GUI/API protection,
books/config claims and a connected 2.1.7 peer. The stopped-v1 backup was uploaded
and separately remotely inspected before activation; it remains retained.
This is not a real-data restore test or permission to run v1 against SQLite.

The approved owned compiler derivatives retain exact Gluetun v3.41.3 and
Stash v0.31.1 source, vendor runtime layers, auxiliary tools, native entrypoint
and application assets while rebuilding the application ELF with Go 1.27.3.
They are not official vendor distributions. Their immutable runtime record
binds the publication, source revision, original factory and actual ELF hash
without rewriting the historical factory audit. Preserve all read-only public
CA guards and require serial live VPN/application, backup and storage acceptance.
The actual compiler and bounded native proofs are distinct from universal
custom/intermediate TLS or forensic certification.

The derivatives were deployed serially in
[PR419](https://github.com/blazepower/rishik-homelab-rke2/pull/419) and
[PR420](https://github.com/blazepower/rishik-homelab-rke2/pull/420).
Actual running PID1 and installed-file hashes match the independently inspected
Go 1.27.3 artifacts. Gluetun passed real VPN health, tunnel/forwarded-port
synchronization and external port openness; Stash retained its original native
version and HTTPS UI. Both preserved claims, mounts and read-only zero-denied
public stores behind freshly uploaded/remotely inspected crash-consistent
backups. The unchanged full-fleet two-worker storage gate passed after each
rollout. Synthetic SQLite preservation remains distinct from real-data restore
testing, and Pod readiness alone is not proof of working VPN forwarding.

The shell-less rreading-glasses image cannot run the shell/Python snapshot init.
Its deployed public-root mitigation instead projects a read-only, hash-referenced
ConfigMap over the existing default bundle. `test-rreading-ca.py --registry`
binds that snapshot to the exact vendor amd64 image and canonical policy, checks
that every unaffected vendor DER identity is preserved, and rejects unexpected
additional factory public stores. That original mitigation did not change the
image, entrypoint, database or credentials, patch compiled Go 1.26.0, or certify
custom TLS. The separate owned-source activation below replaces the active ELF.
The factory-attested source revision
`97dd4a676d435abc400fac93f303a725cde6b0d9` is unavailable from public supplier
Git/archive endpoints; OCI provenance/referrers did not recover an exact source
tree. The same-version compiler derivative remains blocked on that source,
not on missing user approval. Do not silently substitute another revision.
The exact-source request is tracked in
[upstream issue 611](https://github.com/blampe/rreading-glasses/issues/611);
the completed RKE2 maintenance does not resolve that separate blocker.

A separately approved replacement-source line has now been vetted in the
[public GPL fork](https://github.com/blazepower/rreading-glasses/tree/f3855259d93cb8d9899bb940155c571b08c53262).
It preserves the selected signed upstream PR587 base
`ceb4d580f01a920e474ae14d5085ff6809771d22`, adds the narrow queue-owned pending
metric fix tracked in [issue612](https://github.com/blampe/rreading-glasses/issues/612),
and updates advisory-affected networking, cryptography, routing and PostgreSQL
client modules plus their required transitive minima. It is explicitly not
the unavailable factory source or an official vendor release.

The clean Go 1.27.3 ELF is built outside its source tree and copied from the
actual immutable runtime image for acceptance. Original full race tests and
20-repeat controlled queue regressions pass, as do non-root read-only native
trusted/unknown/name-constrained TLS, API metadata and synthetic restored-cache
tests. Both source and actual-binary advisory scans report zero findings after
the dependency updates. The initial older-dependency binary scan failed and
remains preserved; its gate was not waived.

An isolated, normal-authentication, read-only PostgreSQL dump/restore also
preserved real cached book responses. A cold exact factory process read
metadata persisted by the new executable with upstream offline, proving the
tested rollback format. Real records and credentials remained inside the
cluster. No initial stored work rows were present; work persistence was tested
with collision-checked synthetic metadata rather than invented real samples.
An additional physical Longhorn restore was blocked by replica free-space
prechecks, not accepted as successful or diagnosed as database corruption.
The logical rehearsal and its private scratch data were removed without
lowering replica counts, free-space thresholds or storage safeguards.

Publication is gated by the separate accepted-master
`rreading-owned-build.yaml` workflow. Serial production activation additionally
requires a fresh completed retained rollback backup and the unchanged full-fleet
two-worker storage gate. Preserve the vendor distroless runtime, `/main serve`
contract, UID, public CA projection, authentication and original database claim.
These bounded compiler, dependency, TLS and compatibility proofs are not
universal custom/intermediate TLS, hardware or forensic clearance.

The selected production image is
`ghcr.io/blazepower/maintained-rreading-hardcover@sha256:5f2046190d8c17c4af2d507c8137b351280e65623d6725ef8323718711fa8a26`,
published by accepted-master native run
[42531984345](https://github.com/blazepower/rishik-homelab-rke2/actions/runs/42531984345).
Its clean-source Go 1.27.3 ELF SHA-256 is
`cd56d53133445ab655f3265582790e83cef93dc88f5eecdc6917029bb2b7169c`.
The registry publication transiently failed once and passed on a full gated
retry. Native fixtures and live baseline are separate: all historical book
cache rows were expired before activation, so fresh-cache hits are not claimed.
The unchanged original public PEM root bundle is inherited from the pinned
vendor filesystem; the same filtered read-only CA projection remains mounted.

## cert-manager

[cert-manager](https://cert-manager.io/) is deployed to automate TLS certificate management for services exposed via Ingress.

### Configuration

- Deployed via Helm chart from `https://charts.jetstack.io`
- Installed in the `cert-manager` namespace
- Chart version: v1.15.1
- CRDs installed automatically
- Certificate owner references enabled

### Files

- `infrastructure/cert-manager/helmrepository-cert-manager.yaml` - Helm repository source
- `infrastructure/cert-manager/helmrelease-cert-manager.yaml` - Helm release configuration
- `infrastructure/cert-manager/cluster-issuer.yaml` - ClusterIssuer for self-signed CA
- `infrastructure/cert-manager/kustomization.yaml` - Kustomization for cert-manager resources

### ClusterIssuer: cluster-ca

A ClusterIssuer named `cluster-ca` is configured to issue certificates signed by a custom Certificate Authority (CA). This allows all services to use TLS with certificates trusted by browsers/clients that have the CA installed.

### Prerequisites: Creating the CA Secret

Before cert-manager can issue certificates, you must create a TLS secret containing your CA certificate and private key:

```bash
# Generate a new CA (if you don't have one)
openssl genrsa -out ca.key 4096
openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 \
  -out ca.crt -subj "/CN=Homelab CA/O=Homelab"

# Create the secret in the cert-manager namespace
kubectl create secret tls cluster-ca-keypair \
  --cert=ca.crt \
  --key=ca.key \
  -n cert-manager
```

**Important:** Store your CA key securely. Anyone with access to this key can issue trusted certificates for your homelab.

### Using TLS with Ingress Resources

To enable TLS on an Ingress resource, add the cert-manager annotation and TLS configuration:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: my-app
  namespace: my-namespace
  annotations:
    kubernetes.io/ingress.class: traefik
    cert-manager.io/cluster-issuer: cluster-ca
spec:
  rules:
    - host: myapp.homelab
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: my-service
                port:
                  number: 80
  tls:
    - hosts:
        - myapp.homelab
      secretName: myapp-tls
```

cert-manager will automatically:
1. Detect the `cert-manager.io/cluster-issuer` annotation
2. Create a Certificate resource
3. Issue a TLS certificate signed by the CA
4. Store the certificate in the specified secret (`myapp-tls`)
5. Renew the certificate before expiration

### Services Using TLS

The following services are configured with TLS certificates:

| Service | Hostname | TLS Secret |
|---------|----------|------------|
| Grafana | grafana.homelab | grafana-tls |
| Longhorn UI | longhorn.homelab | longhorn-tls |

### Trusting the CA Certificate

To avoid browser security warnings, install the CA certificate on your devices:

#### macOS
```bash
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ca.crt
```

#### Linux (Debian/Ubuntu)
```bash
sudo cp ca.crt /usr/local/share/ca-certificates/homelab-ca.crt
sudo update-ca-certificates
```

#### Windows
```powershell
Import-Certificate -FilePath ca.crt -CertStoreLocation Cert:\LocalMachine\Root
```

### Verifying Certificate Issuance

Check the status of certificates:

```bash
# List all certificates
kubectl get certificates --all-namespaces

# Check certificate details
kubectl describe certificate grafana-tls -n monitoring

# View the actual certificate
kubectl get secret grafana-tls -n monitoring -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -text -noout
```

### Troubleshooting

If certificates are not being issued:

1. Verify the CA secret exists:
   ```bash
   kubectl get secret cluster-ca-keypair -n cert-manager
   ```

2. Check cert-manager logs:
   ```bash
   kubectl logs -n cert-manager deployment/cert-manager
   ```

3. Check the Certificate resource status:
   ```bash
   kubectl describe certificate <name> -n <namespace>
   ```

4. Check for CertificateRequest issues:
   ```bash
   kubectl get certificaterequests --all-namespaces
   ```
