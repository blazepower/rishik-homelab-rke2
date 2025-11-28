# CI/CD Pipeline

This repository includes a comprehensive CI/CD pipeline that runs on all pull requests and pushes to the master branch. The pipeline ensures code quality, security, and correctness before changes are merged.

## Pipeline Checks

| Check | Description | Tool |
|-------|-------------|------|
| **YAML Lint** | Validates YAML syntax and formatting | [yamllint](https://github.com/adrienverge/yamllint) |
| **Kubernetes Validation** | Validates Kubernetes manifests against schemas | [kubeconform](https://github.com/yannh/kubeconform) |
| **Kustomize Build** | Ensures kustomizations build successfully | [kustomize](https://kustomize.io/) |
| **Dry Run Validation** | Builds and validates rendered manifests | [kubeconform](https://github.com/yannh/kubeconform) |
| **Dashboard Validation** | Validates Grafana dashboard JSON in ConfigMaps | Python (yaml, json) |
| **Logging Validation** | Validates Loki/Promtail HelmRelease configurations | Python (yaml) |
| **Secret Detection** | Scans for accidentally committed secrets | [gitleaks](https://github.com/gitleaks/gitleaks) |
| **Security Scan** | Checks Kubernetes security best practices | [kubesec](https://kubesec.io/) |
| **Trivy Scan** | Scans for misconfigurations and vulnerabilities | [Trivy](https://trivy.dev/) |
| **Flux Validation** | Validates Flux GitOps resources | [Flux CLI](https://fluxcd.io/) |

## Configuration Files

- `.github/workflows/ci.yaml` - GitHub Actions workflow definition
- `.yamllint.yaml` - YAML linting configuration
- `.gitleaks.toml` - Secret detection configuration

## New Validation Features

### Dashboard Validation

The pipeline validates all Grafana dashboard ConfigMaps in `infrastructure/monitoring/custom-dashboards/`:

- Ensures embedded JSON is valid
- Checks for proper Grafana dashboard structure (panels/rows)
- Reports warnings for potentially invalid dashboards

### Logging Validation

The pipeline validates Loki and Promtail configurations:

- Validates HelmRelease YAML syntax
- Checks Loki storage configuration and retention settings
- Verifies Promtail client URLs point to Loki
- Ensures all kustomization resources exist

### Dry Run Validation

The pipeline performs a "dry run" by:

1. Building kustomize manifests for apps, infrastructure, logging, and clusters
2. Validating rendered manifests with kubeconform against Kubernetes schemas
3. Reporting validation results with detailed summaries

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

# Dry Run Validation (build + validate rendered manifests)
kustomize build infrastructure --enable-helm > /tmp/manifests.yaml
kubeconform -strict -ignore-missing-schemas -summary /tmp/manifests.yaml

# Dashboard JSON Validation
python3 -c "
import yaml, json
for f in ['infrastructure/monitoring/custom-dashboards/configmap-rke2-control-plane.yaml']:
    data = yaml.safe_load(open(f))
    for key, value in data.get('data', {}).items():
        if key.endswith('.json'):
            json.loads(value)
            print(f'{key} is valid JSON')
"

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
5. **Dashboard Integrity**: Grafana dashboard JSON is validated to prevent broken dashboards
6. **Logging Configuration**: Loki/Promtail settings are validated for proper log collection

## Adding Exceptions

If you need to add exceptions for false positives:

- **yamllint**: Add paths to the `ignore` section in `.yamllint.yaml`
- **gitleaks**: Add patterns to the `allowlist` section in `.gitleaks.toml`
- **kubesec**: Node bootstrap scripts are automatically excluded as they require privileged access
