# rishik-homelab-rke2

Kubernetes homelab configuration managed with RKE2 and Flux GitOps.

## Directory Structure

```
clusters/
└── production/
    └── kustomization.yaml  # Production cluster configuration
infrastructure/
├── namespaces/             # Namespace definitions
└── storage/                # Storage configuration
applications/
└── plex/                   # Plex application
```