# Homepage Helm Chart

A Helm chart for deploying Homepage dashboard in Kubernetes.

## Overview

Homepage is a modern, fully static, fast, secure fully proxied, highly customizable application dashboard with integrations for over 100 services.

## Features

- Kubernetes integration for monitoring cluster resources
- Service discovery and status monitoring
- Customizable widgets (search, datetime, cluster resources)
- RBAC-enabled for secure cluster access
- Configurable service groups and layouts

## Configuration

The chart includes pre-configured service groups for:

- **Media**: Plex, Overseerr, Sonarr, Radarr, Bazarr, Prowlarr, SABnzbd
- **Documents & Books**: Paperless-ngx, Calibre-web, Bookshelf, Kindle-sender, RReading-glasses
- **Productivity**: Kaneo, Homebox, Sure-finance

## Values

See `values.yaml` for all configuration options.

Key configuration sections:
- `image`: Container image settings
- `service`: Service type and ports
- `resources`: CPU and memory limits
- `config.services`: Service definitions and URLs
- `config.widgets`: Dashboard widgets
- `config.settings`: General settings and theme

## Security

The chart follows security best practices:
- Runs as non-root user
- Drops all capabilities
- Uses ClusterRole with minimal required permissions
- Supports RBAC for Kubernetes API access
