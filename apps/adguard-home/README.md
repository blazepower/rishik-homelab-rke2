# AdGuard Home

AdGuard Home is a network-wide DNS ad-blocker and privacy protection service. It acts as a DNS server that blocks ads, trackers, and malicious domains before they reach your devices.

## Features

- **Network-wide ad blocking**: Blocks ads on all devices connected to your network
- **Privacy protection**: Blocks tracking and analytics domains
- **Malware protection**: Blocks known malicious domains
- **Customizable filtering**: Configure custom blocklists and whitelists
- **Query logging**: View DNS queries and statistics
- **Parental controls**: Block adult content and specific categories

## Deployment

### Architecture

- **Namespace**: `adguard-home`
- **Image**: `adguard/adguardhome:v0.107.52`
- **Resources**: 
  - Requests: 50m CPU, 128Mi memory
  - Limits: 200m CPU, 256Mi memory

### Services

1. **Web UI Service** (`adguard-home-web`):
   - Port: 3000
   - Type: ClusterIP
   - Used for accessing the AdGuard Home web interface

2. **DNS Service** (`adguard-home-dns`):
   - Port: 53 (TCP and UDP)
   - Type: ClusterIP
   - Used for DNS queries

### Storage

- **Config Volume**: 1Gi (Longhorn storage class)
  - Mounted at: `/opt/adguardhome/conf`
  - Stores AdGuard Home configuration
  
- **Work Volume**: 2Gi (Longhorn storage class)
  - Mounted at: `/opt/adguardhome/work`
  - Stores query logs and statistics

Both volumes have the `helm.sh/resource-policy: keep` annotation to preserve data during helm uninstalls.

### Access

- **Local (via Traefik)**: https://adguard.homelab
- **Remote (via Tailscale)**: https://adguard

### Security

- **Security Context**: 
  - Runs as root (required for binding to port 53)
  - NET_BIND_SERVICE capability to bind to privileged port
  
- **Network Policy**:
  - Allows Traefik ingress controller to reach web UI (port 3000)
  - Allows internal namespace communication
  - Allows DNS queries from all namespaces (port 53 TCP/UDP)

- **TLS**: 
  - Certificate managed by cert-manager
  - Issuer: cluster-ca

## Initial Setup

On first access to the web UI, AdGuard Home will run an initial setup wizard:

1. Access https://adguard.homelab (or https://adguard via Tailscale)
2. Configure admin username and password
3. Choose DNS server listening interface (default: all interfaces)
4. Configure upstream DNS servers (e.g., 8.8.8.8, 1.1.1.1)
5. Complete setup and start using AdGuard Home

## Configuration

### Upstream DNS Servers

Common options:
- Google DNS: `8.8.8.8`, `8.8.4.4`
- Cloudflare DNS: `1.1.1.1`, `1.0.0.1`
- Quad9 DNS: `9.9.9.9`, `149.112.112.112`

### Blocklists

AdGuard Home comes with default blocklists. You can add more from:
- AdGuard filters
- OISD blocklists
- Steven Black's hosts
- Custom lists

### Using as DNS Server

To use AdGuard Home as your DNS server:

1. **For pods in the cluster**: Configure pod DNS to point to the service:
   ```yaml
   dnsPolicy: None
   dnsConfig:
     nameservers:
       - adguard-home-dns.adguard-home.svc.cluster.local
   ```

2. **For devices on your network**: Update router DHCP settings or device network settings to use AdGuard Home's IP address as DNS server.

## Monitoring

- View DNS query logs in the web UI
- Check statistics and top blocked domains
- Monitor resource usage via Kubernetes metrics

## Troubleshooting

### Pod not starting
- Check PVC status: `kubectl get pvc -n adguard-home`
- Check pod logs: `kubectl logs -n adguard-home -l app.kubernetes.io/name=adguard-home`

### DNS not working
- Verify service is running: `kubectl get svc -n adguard-home`
- Check network policy: `kubectl get networkpolicy -n adguard-home`
- Test DNS resolution: `nslookup google.com <service-ip>`

### Web UI not accessible
- Check ingress: `kubectl get ingress -n adguard-home`
- Verify certificate: `kubectl get certificate -n adguard-home`
- Check Traefik logs for routing issues

## References

- [AdGuard Home Documentation](https://github.com/AdguardTeam/AdGuardHome/wiki)
- [Official Website](https://adguard.com/adguard-home.html)
- [Docker Hub](https://hub.docker.com/r/adguard/adguardhome)
