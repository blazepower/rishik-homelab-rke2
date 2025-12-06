# Sealed Secrets

## Overview

[Sealed Secrets](https://sealed-secrets.netlify.app/) provides a secure way to manage Kubernetes secrets in a GitOps workflow. It allows you to encrypt secrets that can only be decrypted by the Sealed Secrets controller running in your cluster, making it safe to store encrypted secrets in Git.

### How It Works

1. The Sealed Secrets controller generates a public/private key pair when first deployed
2. You use the `kubeseal` CLI tool to encrypt secrets using the controller's public certificate
3. The encrypted "SealedSecret" resources are safe to commit to Git
4. The controller watches for SealedSecret resources and decrypts them into regular Kubernetes Secrets
5. Only the controller (with the private key) can decrypt the secrets

### Benefits for This Homelab

- **GitOps-friendly**: Safely store encrypted secrets alongside your infrastructure code
- **Cluster-specific encryption**: Secrets can only be decrypted by this specific cluster
- **No external dependencies**: Works entirely within Kubernetes, no external secret managers needed
- **Flux integration**: Works seamlessly with the existing Flux GitOps workflow

## Configuration

### Important: Controller Configuration

When using the `kubeseal` CLI tool, you **must** specify the controller name and namespace for this cluster:

- **Controller Name**: `sealed-secrets`
- **Controller Namespace**: `flux-system`

All `kubeseal` commands in this documentation use these values via the flags:
- `--controller-name=sealed-secrets`
- `--controller-namespace=flux-system`

### Deployment Details

- **Chart**: sealed-secrets from bitnami-labs
- **Chart version**: 2.17.0
- **Namespace**: flux-system
- **Helm repository**: https://bitnami-labs.github.io/sealed-secrets

### Resource Limits

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Controller | 25m | 100m | 64Mi | 128Mi |

### Files

- `infrastructure/sealed-secrets/helmrepository-sealed-secrets.yaml` - Helm repository source
- `infrastructure/sealed-secrets/helmrelease-sealed-secrets.yaml` - Helm release configuration
- `infrastructure/sealed-secrets/kustomization.yaml` - Kustomization for sealed-secrets resources

## Installation Guide

### Prerequisites: kubeseal CLI

The `kubeseal` CLI is required to encrypt secrets. Install it on your local machine:

#### macOS

```bash
brew install kubeseal
```

#### Linux

```bash
# Download the latest release
KUBESEAL_VERSION=$(curl -s https://api.github.com/repos/bitnami-labs/sealed-secrets/releases/latest | grep tag_name | cut -d '"' -f 4 | cut -d 'v' -f 2)
wget "https://github.com/bitnami-labs/sealed-secrets/releases/download/v${KUBESEAL_VERSION}/kubeseal-${KUBESEAL_VERSION}-linux-amd64.tar.gz"
tar -xvzf kubeseal-${KUBESEAL_VERSION}-linux-amd64.tar.gz kubeseal
sudo install -m 755 kubeseal /usr/local/bin/kubeseal
```

#### Windows

```powershell
# Using scoop
scoop install kubeseal

# Or download manually from GitHub releases
# https://github.com/bitnami-labs/sealed-secrets/releases
```

### Verify Controller Installation

After Flux deploys the sealed-secrets controller, verify it's running:

```bash
# Check the controller pod
kubectl get pods -n flux-system -l app.kubernetes.io/name=sealed-secrets

# Expected output:
# NAME                              READY   STATUS    RESTARTS   AGE
# sealed-secrets-xxxxxxxxxx-xxxxx   1/1     Running   0          5m

# Check the controller logs
kubectl logs -n flux-system -l app.kubernetes.io/name=sealed-secrets
```

### Fetch the Controller's Public Certificate

Before creating sealed secrets, fetch the controller's public certificate:

```bash
# Fetch and save the certificate
kubeseal --fetch-cert \
  --controller-name=sealed-secrets \
  --controller-namespace=flux-system \
  > /tmp/sealed-secrets-cert.pem

# Verify the certificate
openssl x509 -in /tmp/sealed-secrets-cert.pem -noout -text | head -20
```

## Usage Guide

### Example 1: Creating a New Sealed Secret from Scratch

This example creates a sealed secret for an application that needs database credentials.

#### Step 1: Create a regular Kubernetes secret YAML

```bash
# Create a secret manifest (don't apply this directly!)
cat > /tmp/db-credentials.yaml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: db-credentials
  namespace: my-app
type: Opaque
stringData:
  username: myuser
  password: supersecretpassword123
EOF
```

#### Step 2: Seal the secret

```bash
# Option A: Using the certificate file
kubeseal --format yaml \
  --cert /tmp/sealed-secrets-cert.pem \
  < /tmp/db-credentials.yaml \
  > apps/my-app/sealedsecret-db-credentials.yaml

# Option B: Fetching certificate directly from the cluster
kubeseal --format yaml \
  --controller-name=sealed-secrets \
  --controller-namespace=flux-system \
  < /tmp/db-credentials.yaml \
  > apps/my-app/sealedsecret-db-credentials.yaml
```

#### Step 3: Clean up and commit

```bash
# Remove the unencrypted secret
rm /tmp/db-credentials.yaml

# Commit the sealed secret to Git
git add apps/my-app/sealedsecret-db-credentials.yaml
git commit -m "Add sealed secret for db credentials"
git push
```

#### Step 4: Verify the secret was created

```bash
# Wait for Flux to reconcile, then check
kubectl get sealedsecret -n my-app
kubectl get secret db-credentials -n my-app
```

### Example 2: Converting Existing Secrets to SealedSecrets

If you have existing secrets in the cluster that should be managed via GitOps:

#### Step 1: Export the existing secret

```bash
# Export without cluster-specific metadata
kubectl get secret grafana-admin-credentials -n monitoring -o yaml | \
  kubectl neat > /tmp/grafana-secret.yaml

# If kubectl-neat is not installed, manually remove these fields:
# - metadata.creationTimestamp
# - metadata.resourceVersion
# - metadata.uid
# - metadata.annotations["kubectl.kubernetes.io/last-applied-configuration"]
```

#### Step 2: Seal the exported secret

```bash
kubeseal --format yaml \
  --controller-name=sealed-secrets \
  --controller-namespace=flux-system \
  < /tmp/grafana-secret.yaml \
  > infrastructure/monitoring/sealedsecret-grafana-credentials.yaml
```

#### Step 3: Update kustomization and commit

```bash
# Add to the monitoring kustomization.yaml
# Then commit and push
rm /tmp/grafana-secret.yaml
git add infrastructure/monitoring/sealedsecret-grafana-credentials.yaml
git commit -m "Convert grafana credentials to sealed secret"
git push
```

### Example 3: Creating Sealed Secrets for Different Scopes

SealedSecrets supports three scopes that control where the secret can be used:

#### Strict Scope (default)

The secret is bound to a specific name AND namespace. Most secure option.

```bash
kubeseal --format yaml \
  --scope strict \
  --controller-name=sealed-secrets \
  --controller-namespace=flux-system \
  < secret.yaml > sealedsecret.yaml
```

#### Namespace-wide Scope

The secret can have any name but must stay in the specified namespace.

```bash
kubeseal --format yaml \
  --scope namespace-wide \
  --controller-name=sealed-secrets \
  --controller-namespace=flux-system \
  < secret.yaml > sealedsecret.yaml
```

#### Cluster-wide Scope

The secret can be moved to any namespace with any name. Least restrictive.

```bash
kubeseal --format yaml \
  --scope cluster-wide \
  --controller-name=sealed-secrets \
  --controller-namespace=flux-system \
  < secret.yaml > sealedsecret.yaml
```

### Example 4: Updating Sealed Secrets

To update an existing sealed secret, you must re-seal the entire secret:

#### Step 1: Create the updated secret manifest

```bash
cat > /tmp/updated-secret.yaml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: db-credentials
  namespace: my-app
type: Opaque
stringData:
  username: myuser
  password: newpassword456
EOF
```

#### Step 2: Re-seal and replace

```bash
kubeseal --format yaml \
  --controller-name=sealed-secrets \
  --controller-namespace=flux-system \
  < /tmp/updated-secret.yaml \
  > apps/my-app/sealedsecret-db-credentials.yaml

rm /tmp/updated-secret.yaml
git add apps/my-app/sealedsecret-db-credentials.yaml
git commit -m "Update db credentials"
git push
```

## Migrating Existing Secrets

### Grafana Admin Credentials

The Grafana admin password is stored in `grafana-admin-credentials` in the monitoring namespace:

```bash
# Export the current secret
kubectl get secret grafana-admin-credentials -n monitoring -o yaml > /tmp/grafana-secret.yaml

# Edit to remove cluster metadata (creationTimestamp, resourceVersion, uid)
# Then seal it
kubeseal --format yaml \
  --controller-name=sealed-secrets \
  --controller-namespace=flux-system \
  < /tmp/grafana-secret.yaml \
  > infrastructure/monitoring/sealedsecret-grafana-admin-credentials.yaml

# Clean up
rm /tmp/grafana-secret.yaml
```

### TLS CA Keypair

The cluster CA keypair is stored in `cluster-ca-keypair` in the cert-manager namespace:

```bash
# Export the current secret
kubectl get secret cluster-ca-keypair -n cert-manager -o yaml > /tmp/ca-keypair.yaml

# Edit to remove cluster metadata
# Then seal it
kubeseal --format yaml \
  --controller-name=sealed-secrets \
  --controller-namespace=flux-system \
  < /tmp/ca-keypair.yaml \
  > infrastructure/cert-manager/sealedsecret-cluster-ca-keypair.yaml

# Clean up
rm /tmp/ca-keypair.yaml
```

## Backup and Recovery

### Why Backup is Critical

The sealed-secrets controller generates a unique private key when first deployed. This key is stored as a secret in the cluster. **If you lose this key, you cannot decrypt any existing SealedSecrets.**

When rebuilding the cluster, you must restore this key or all your sealed secrets will become unrecoverable.

### Backing Up the Sealing Key

```bash
# Export the sealing key
kubectl get secret -n flux-system -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml > sealed-secrets-key-backup.yaml

# Store this file securely OFFLINE:
# - Encrypted USB drive
# - Password manager
# - Secure cloud storage (encrypted)
# - Hardware security module

# NEVER commit this file to Git!
```

### Restoring the Sealing Key

When rebuilding the cluster, restore the key BEFORE deploying the sealed-secrets controller:

```bash
# Create the flux-system namespace if it doesn't exist
kubectl create namespace flux-system --dry-run=client -o yaml | kubectl apply -f -

# Restore the sealing key
kubectl apply -f sealed-secrets-key-backup.yaml

# Then let Flux deploy the sealed-secrets controller
# It will detect and use the existing key
```

### Key Rotation

The controller can rotate keys while keeping old keys to decrypt existing secrets:

```bash
# Trigger key rotation
kubectl annotate secret -n flux-system -l sealedsecrets.bitnami.com/sealed-secrets-key \
  sealedsecrets.bitnami.com/sealed-secrets-key-rotation=true

# Backup the new key immediately after rotation
kubectl get secret -n flux-system -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml > sealed-secrets-key-backup-rotated.yaml
```

## Troubleshooting

### Controller Not Unsealing Secrets

1. **Check controller logs**:
   ```bash
   kubectl logs -n flux-system -l app.kubernetes.io/name=sealed-secrets
   ```

2. **Verify the SealedSecret exists**:
   ```bash
   kubectl get sealedsecret -A
   ```

3. **Check for events on the SealedSecret**:
   ```bash
   kubectl describe sealedsecret <name> -n <namespace>
   ```

### Certificate Fetch Failures

If `kubeseal --fetch-cert` fails:

1. **Verify the controller is running**:
   ```bash
   kubectl get pods -n flux-system -l app.kubernetes.io/name=sealed-secrets
   ```

2. **Check network connectivity**:
   ```bash
   kubectl get svc -n flux-system sealed-secrets
   ```

3. **Use kubectl proxy as fallback**:
   ```bash
   kubectl port-forward -n flux-system svc/sealed-secrets 8080:8080 &
   curl http://localhost:8080/v1/cert.pem > cert.pem
   ```

#### NetworkPolicy Requirements

If `kubeseal --fetch-cert` returns a 502 Bad Gateway error:

```
error: cannot fetch certificate: error trying to reach service: proxy error from 127.0.0.1:9345 while dialing 10.42.x.x:8080, code 502: 502 Bad Gateway
```

This indicates that NetworkPolicies are blocking the Kubernetes API server proxy from reaching the sealed-secrets pod. The API server proxy connects directly to pod IPs, which means it bypasses the Service and requires explicit NetworkPolicy rules.

**Solution:** Ensure the `allow-sealed-secrets` NetworkPolicy exists in `flux-system` namespace. This policy is located at:
- `infrastructure/policies/network-policies/allow-sealed-secrets.yaml`

The policy allows:
- Ingress to sealed-secrets pod on port 8080 from the API server/node network
- Ingress on port 8081 for Prometheus metrics scraping

See [docs/policies.md](policies.md) for more details on NetworkPolicies.

### Scope/Namespace Mismatches

If you see "no key could decrypt secret" errors:

1. **Check the scope annotation on the SealedSecret**:
   ```bash
   kubectl get sealedsecret <name> -n <namespace> -o yaml | grep -A5 annotations
   ```

2. **Verify namespace and name match** what was used during sealing (for strict scope)

3. **Re-seal with correct scope** if needed:
   ```bash
   kubeseal --format yaml --scope namespace-wide ...
   ```

### Checking Controller Logs

```bash
# Stream logs
kubectl logs -f -n flux-system -l app.kubernetes.io/name=sealed-secrets

# Get recent logs
kubectl logs -n flux-system -l app.kubernetes.io/name=sealed-secrets --tail=100
```

### Verifying Certificate Validity

```bash
# Check certificate expiration
kubeseal --fetch-cert \
  --controller-name=sealed-secrets \
  --controller-namespace=flux-system | \
  openssl x509 -noout -dates
```

## Security Best Practices

### Never Commit Unsealed Secrets

- Always use `/tmp` or a secure temporary directory for plaintext secrets
- Delete plaintext secrets immediately after sealing
- The repository has gitleaks configured to detect accidental commits

### Backup the Sealing Key Securely

- Store backup offline (encrypted USB, password manager)
- Never commit the sealing key to Git
- Test restoration procedure periodically

### Use Appropriate Scopes

| Use Case | Recommended Scope |
|----------|------------------|
| Application-specific credentials | strict (default) |
| Shared namespace secrets | namespace-wide |
| Cluster-wide shared secrets | cluster-wide |

### Rotate Secrets Periodically

- Rotate sensitive credentials on a regular schedule
- Update sealed secrets after rotation
- Consider key rotation for the controller annually

### Use gitleaks

This repository has gitleaks configured in CI to prevent accidental commits of plaintext secrets:

```bash
# Run locally before committing
gitleaks detect --source .
```

## Integration with Existing Stack

### Flux CD

SealedSecrets integrates naturally with Flux:

1. Flux deploys the sealed-secrets controller via HelmRelease
2. SealedSecret resources can be included in any Kustomization
3. When Flux applies a SealedSecret, the controller automatically creates the corresponding Secret
4. The reconciliation is automatic and continuous

### cert-manager

SealedSecrets can manage the CA keypair for cert-manager:

1. Export and seal the `cluster-ca-keypair` secret
2. Commit the SealedSecret to Git
3. Remove manual secret creation from cluster setup procedures
4. The CA keypair will be automatically created when the cluster is bootstrapped

### Monitoring Stack

The sealed-secrets controller exposes Prometheus metrics:

- The HelmRelease enables ServiceMonitor for automatic scraping
- Metrics are available at `/metrics` on the controller
- Grafana can visualize unseal operations and errors

Metrics include:
- `sealed_secrets_controller_unseal_requests_total` - Total unseal attempts
- `sealed_secrets_controller_unseal_errors_total` - Failed unseal attempts
