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

## Tailscale Operator

[Tailscale](https://tailscale.com/) provides secure remote access to cluster services via an encrypted mesh network (tailnet) without exposing services to the public internet.

### Overview

The Tailscale Kubernetes Operator is deployed to manage ingress resources and provide automatic HTTPS with LetsEncrypt certificates for services exposed on the tailnet.

### Configuration

- Deployed via Helm chart from `https://pkgs.tailscale.com/helmcharts`
- Installed in the `tailscale` namespace
- Chart version: 1.90.9
- OAuth credentials stored in SealedSecret
- ProxyGroup type: egress with 2 replicas
- Hostname prefix: `homelab`
- Tailnet domain: `tail4217c.ts.net`

### Resource Configuration

The Tailscale Operator uses minimal resources for efficient operation:

| Component | Description |
|-----------|-------------|
| ProxyGroup | `homelab-ingress` with 2 replicas for HA |
| Tags | `tag:k8s-operator` for ACL management |
| Type | Egress proxy for ingress traffic |

### Files

- `infrastructure/tailscale/helmrepository.yaml` - Helm repository source
- `infrastructure/tailscale/helmrelease.yaml` - Helm release configuration with OAuth credentials
- `infrastructure/tailscale/proxygroup.yaml` - ProxyGroup configuration for ingress proxies
- `infrastructure/tailscale/sealedsecret-tailscale-credentials.yaml` - OAuth client credentials
- `infrastructure/tailscale/kustomization.yaml` - Kustomization for Tailscale resources

### Services Exposed via Tailscale

Services with Tailscale ingresses are accessible with automatic HTTPS via LetsEncrypt certificates:

#### Media Services
- Overseerr: `https://overseerr.tail4217c.ts.net`
- Sonarr: `https://sonarr.tail4217c.ts.net`
- Radarr: `https://radarr.tail4217c.ts.net`
- Bazarr: `https://bazarr.tail4217c.ts.net`
- Prowlarr: `https://prowlarr.tail4217c.ts.net`
- SABnzbd: `https://sabnzbd.tail4217c.ts.net`

#### Documents & Books
- Paperless-ngx: `https://paperless.tail4217c.ts.net`
- Calibre-Web: `https://calibre.tail4217c.ts.net`
- Bookshelf: `https://bookshelf.tail4217c.ts.net`

#### Productivity
- Kaneo: `https://kaneo.tail4217c.ts.net`
- Homebox: `https://homebox.tail4217c.ts.net`
- Sure Finance: `https://sure.tail4217c.ts.net`

#### Infrastructure
- Grafana: `https://grafana.tail4217c.ts.net`
- Longhorn: `https://longhorn.tail4217c.ts.net`
- AdGuard Home: `https://adguard.tail4217c.ts.net`
- Homepage: `https://home.tail4217c.ts.net`

### Usage

To expose a service via Tailscale:

1. **Create a Tailscale Ingress resource**:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: myapp-tailscale
  namespace: my-namespace
  annotations:
    tailscale.com/tags: "tag:k8s-operator"
spec:
  ingressClassName: tailscale
  defaultBackend:
    service:
      name: my-service
      port:
        number: 8080
  tls:
    - hosts:
        - myapp
```

2. **The Tailscale Operator will**:
   - Create a proxy pod for your service
   - Assign it a hostname on your tailnet: `https://myapp.tail4217c.ts.net`
   - Provision a LetsEncrypt certificate automatically
   - Make it accessible only to devices on your tailnet

### Security

- **Zero Trust**: Only devices authenticated to your tailnet can access services
- **Automatic HTTPS**: LetsEncrypt certificates provisioned and renewed automatically
- **No Public Exposure**: Services remain private and not exposed to the internet
- **ACL Management**: Use `tag:k8s-operator` for granular access control
- **Network Policies**: Each service has appropriate network policies for pod-to-pod communication

### Benefits

- ✅ Secure remote access from any device on your tailnet
- ✅ No VPN configuration or port forwarding required
- ✅ Automatic certificate management
- ✅ Multi-device support (phone, laptop, tablet)
- ✅ Simple setup - just install Tailscale client and connect

### Troubleshooting

Check Tailscale Operator status:
```bash
kubectl get pods -n tailscale
kubectl logs -n tailscale -l app=operator
```

View ProxyGroup status:
```bash
kubectl get proxygroup -n tailscale
kubectl describe proxygroup homelab-ingress -n tailscale
```

List Tailscale ingresses:
```bash
kubectl get ingress --all-namespaces -l tailscale.com/parent-resource-type
```
