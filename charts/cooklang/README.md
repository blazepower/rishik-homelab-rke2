# Cooklang Helm Chart

A Helm chart for deploying Cooklang recipe server.

## Description

Cooklang is a markup language for recipes. This chart deploys the CookCLI server which provides a web interface to browse and view recipes written in Cooklang format.

## Features

- Distroless container image for security
- Persistent storage for recipes
- Non-root user execution
- Read-only root filesystem

## Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `image.repository` | Container image repository | `ghcr.io/blazepower/cooklang` |
| `image.tag` | Container image tag | `0.10.1` |
| `persistence.enabled` | Enable persistent storage | `true` |
| `persistence.size` | PVC size | `1Gi` |
| `persistence.storageClass` | Storage class | `longhorn` |

## Usage

Recipes should be placed in the `/recipes` directory (mounted via PVC).

Recipe files use the `.cook` extension and follow the Cooklang syntax:
```
>> servings: 4

Preheat the oven to 180°C.

Mix @flour{2%cups} with @sugar{1%cup} and @eggs{2}.
```

See https://cooklang.org for more information.
