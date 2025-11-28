# Networking

## Traefik Ingress Controller

[Traefik](https://traefik.io/) is deployed as the default ingress controller for the cluster, providing HTTPS routing, load-balancing, middleware support, and future Let's Encrypt integration.

### Configuration

- Deployed via Helm chart from `https://traefik.github.io/charts`
- Installed in the `kube-system` namespace
- Chart version: 25.0.0
- Deployment type: DaemonSet (runs on all nodes, no external load balancer required)
- Exposes ports 80 (HTTP) and 443 (HTTPS) via hostPorts
- Service type: ClusterIP
- Set as the default IngressClass

### Files

- `infrastructure/networking/traefik/helmrepository-traefik.yaml` - Helm repository source
- `infrastructure/networking/traefik/helmrelease-traefik.yaml` - Helm release configuration
- `infrastructure/networking/traefik/kustomization.yaml` - Kustomization for Traefik resources

### Usage

Once deployed, Traefik will be available as the default ingress controller. Create Ingress resources to expose your services:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: my-app
  namespace: my-namespace
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
```

**Note:** Ensure you have DNS entries pointing your ingress hostnames to your node IPs where Traefik is running.
