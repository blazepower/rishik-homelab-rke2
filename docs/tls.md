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
preparation report.

Bookshelf, qBittorrent/Gluetun and Seerr system stores reuse the shared media
guard. Their separate Linux gate checks original isolated startup and
read-only public stores; Seerr also checks native default/bundled/system root
inventories. This is not .NET/Qt/custom TLS or credentialed VPN clearance.
Gluetun's Go 1.25.11 remains below the incident floor.

The owned Kindle Sender has a pinned Go 1.27.3 rebuild and non-publishing
Linux acceptance workflow. Native tests use the exact eight public SPKIs and
require certificate-policy rejection, not an arbitrary handshake failure.
The maintained static-CGO/SQLite image must pass before publication and a
separately reviewed immutable release pin. Its deployed image is unchanged.
Explicit custom TLS contexts and other compiled Go gaps remain outside
these overlays. Historical audits remain timestamped pre-deployment baselines.

### Controlled activation

The prepared Radarr, Calibre-Web, Bazarr, Prowlarr, SABnzbd, Sure, Bookshelf,
qBittorrent and Seerr releases are explicitly suspended in Git. This prevents
merging the preparation from starting nine independent stateful rollouts
together. Do not remove these
holds during external storage maintenance. After the storage owner confirms
completion and a fresh sustained safety check passes, activate one release at
a time through Git, update its CI hold assertion, and verify actual startup,
trust rejection, authentication and storage before activating the next.
Preparation reports and passing CI do not authorize deployment.

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
