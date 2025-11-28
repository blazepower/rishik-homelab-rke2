# Monitoring

## kube-prometheus-stack

[kube-prometheus-stack](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack) is deployed to provide comprehensive cluster monitoring with Prometheus, Grafana, and Alertmanager.

### Configuration

- Deployed via Helm chart from `https://prometheus-community.github.io/helm-charts`
- Installed in the `monitoring` namespace
- Prometheus retention: 7 days
- Scrape interval: 30 seconds
- Grafana exposed via Traefik ingress at `grafana.homelab`
- Alertmanager enabled

### Files

- `infrastructure/monitoring/helmrepository-prometheus-community.yaml` - Helm repository source
- `infrastructure/monitoring/helmrelease-kube-prometheus-stack.yaml` - Helm release configuration
- `infrastructure/monitoring/ingress-grafana.yaml` - Ingress for Grafana UI
- `infrastructure/monitoring/kustomization.yaml` - Kustomization for monitoring resources

### Accessing Grafana

Grafana is accessible via the Traefik ingress at http://grafana.homelab. Ensure you have a DNS entry pointing `grafana.homelab` to your Traefik ingress controller's IP address.

Alternatively, use port-forwarding:
```bash
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
```
Then open http://localhost:3000 in your browser.

### Customization

To pin a specific chart version, uncomment and set the `version` field in `helmrelease-kube-prometheus-stack.yaml`.
