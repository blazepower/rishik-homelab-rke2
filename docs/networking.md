# Networking

## Traefik Ingress Controller

[Traefik](https://traefik.io/) is deployed as the default ingress controller for the cluster, providing HTTPS routing, load-balancing, middleware support, and TLS termination via cert-manager.

### Configuration

- Deployed via Helm chart from `https://traefik.github.io/charts`
- Installed in the `kube-system` namespace
- Chart version: 25.0.0
- Deployment type: DaemonSet (runs on all nodes, no external load balancer required)
- Exposes ports 80 (HTTP) and 443 (HTTPS) via hostPorts
- Service type: ClusterIP
- Set as the default IngressClass

### Resource Limits

Traefik has resource limits configured to prevent resource exhaustion:

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit |
|-----------|-------------|-----------|----------------|--------------|
| Traefik   | 50m         | 200m      | 64Mi           | 256Mi        |

### Files

- `infrastructure/networking/traefik/helmrepository-traefik.yaml` - Helm repository source
- `infrastructure/networking/traefik/helmrelease-traefik.yaml` - Helm release configuration
- `infrastructure/networking/traefik/kustomization.yaml` - Kustomization for Traefik resources

### TLS/HTTPS with cert-manager

All Ingress resources use TLS certificates issued by cert-manager. To enable TLS on an Ingress:

1. Add the cert-manager annotation
2. Configure the TLS section with hosts and secret name

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

cert-manager will automatically issue a TLS certificate signed by the `cluster-ca` ClusterIssuer and store it in the specified secret.

See [docs/tls.md](tls.md) for detailed cert-manager configuration and CA setup instructions.

### Services Exposed via HTTPS

The following services are exposed with TLS:

| Service | Hostname | Ingress Location |
|---------|----------|------------------|
| Grafana | https://grafana.homelab | `infrastructure/monitoring/ingress-grafana.yaml` |
| Longhorn UI | https://longhorn.homelab | `infrastructure/storage/longhorn/ingress-longhorn.yaml` |

### Usage

Once deployed, Traefik will be available as the default ingress controller. Create Ingress resources to expose your services.

**Note:** Ensure you have:
1. DNS entries pointing your ingress hostnames to your node IPs where Traefik is running
2. The homelab CA certificate installed on your devices to avoid browser security warnings (see [docs/tls.md](tls.md))
