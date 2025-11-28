# CI/CD Pipeline

This repository includes a comprehensive CI/CD pipeline that runs on all pull requests and pushes to the master branch. The pipeline ensures code quality, security, and correctness before changes are merged.

## Pipeline Checks

| Check | Description | Tool |
|-------|-------------|------|
| **YAML Lint** | Validates YAML syntax and formatting | [yamllint](https://github.com/adrienverge/yamllint) |
| **Kubernetes Validation** | Validates Kubernetes manifests against schemas | [kubeconform](https://github.com/yannh/kubeconform) |
| **Kustomize Build** | Ensures kustomizations build successfully | [kustomize](https://kustomize.io/) |
| **Secret Detection** | Scans for accidentally committed secrets | [gitleaks](https://github.com/gitleaks/gitleaks) |
| **Security Scan** | Checks Kubernetes security best practices | [kubesec](https://kubesec.io/) |
| **Trivy Scan** | Scans for misconfigurations and vulnerabilities | [Trivy](https://trivy.dev/) |
| **Flux Validation** | Validates Flux GitOps resources | [Flux CLI](https://fluxcd.io/) |

## Configuration Files

- `.github/workflows/ci.yaml` - GitHub Actions workflow definition
- `.yamllint.yaml` - YAML linting configuration
- `.gitleaks.toml` - Secret detection configuration

## Running Checks Locally

You can run the same checks locally before pushing:

```bash
# YAML Lint
pip install yamllint
yamllint -c .yamllint.yaml .

# Kubernetes Validation (with Flux schemas)
mkdir -p /tmp/flux-schemas
curl -sL https://github.com/fluxcd/flux2/releases/latest/download/crd-schemas.tar.gz | tar zxf - -C /tmp/flux-schemas
find apps -name '*.yaml' -type f ! -name 'kustomization.yaml' -exec kubeconform \
  -strict -ignore-missing-schemas \
  -schema-location default \
  -schema-location '/tmp/flux-schemas/{{ .ResourceKind }}{{ .KindSuffix }}.json' \
  {} \;

# Kustomize Build
kustomize build apps --enable-helm > /dev/null
kustomize build infrastructure --enable-helm > /dev/null
kustomize build clusters/production --enable-helm > /dev/null

# Secret Detection
gitleaks detect --config .gitleaks.toml --source . --verbose

# Trivy Config Scan
trivy config . --severity HIGH,CRITICAL
```

## Security Best Practices

The pipeline enforces several security best practices:

1. **No Secrets in Code**: The gitleaks scanner detects accidentally committed secrets, API keys, and credentials
2. **Kubernetes Security**: kubesec validates that workloads follow security best practices (non-root users, read-only filesystems, etc.)
3. **Configuration Scanning**: Trivy scans for misconfigurations in Kubernetes manifests
4. **Valid Manifests**: kubeconform ensures all manifests are valid Kubernetes resources

## Adding Exceptions

If you need to add exceptions for false positives:

- **yamllint**: Add paths to the `ignore` section in `.yamllint.yaml`
- **gitleaks**: Add patterns to the `allowlist` section in `.gitleaks.toml`
- **kubesec**: Node bootstrap scripts are automatically excluded as they require privileged access
