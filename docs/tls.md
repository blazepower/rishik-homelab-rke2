# TLS Certificate Management

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
