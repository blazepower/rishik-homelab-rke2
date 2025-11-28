#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="cert-manager"
SECRET_NAME="cluster-ca-keypair"

echo "=== Rotating Cluster CA ==="

# 1. Backup old CA
timestamp=$(date +%Y%m%d-%H%M)
mkdir -p ca-backups
cp cluster-ca.key "ca-backups/cluster-ca-${timestamp}.key"
cp cluster-ca.crt "ca-backups/cluster-ca-${timestamp}.crt"
echo "Backup stored at ca-backups/cluster-ca-${timestamp}.{crt,key}"

# 2. Generate new CA key + cert
openssl genrsa -out cluster-ca.key 4096
openssl req -x509 -new -nodes \
  -key cluster-ca.key \
  -sha256 \
  -days 3650 \
  -subj "/CN=Rishik-Homelab-CA" \
  -out cluster-ca.crt

echo "=== Creating Kubernetes CA Secret ==="

kubectl -n $NAMESPACE delete secret $SECRET_NAME --ignore-not-found=true

kubectl -n $NAMESPACE create secret tls $SECRET_NAME \
  --cert=cluster-ca.crt \
  --key=cluster-ca.key

echo "=== Forcing renewal of all certificates ==="
kubectl get certificates --all-namespaces -o json \
  | jq -r '.items[].metadata | [.namespace, .name] | @tsv' \
  | while IFS=$'\t' read -r ns name; do
        echo "Renewing cert $ns/$name"
        kubectl -n "$ns" annotate certificate "$name" cert-manager.io/renew-reason="ca-rotation" --overwrite
    done

echo "=== CA Rotation Complete ==="
echo "All certs will re-issue automatically."

