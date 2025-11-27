#!/bin/bash
set -e

echo "[RKE2 BOOTSTRAP] Installing open-iscsi..."

if ! dpkg -s open-iscsi >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y open-iscsi
fi

systemctl enable iscsid
systemctl start iscsid

echo "[RKE2 BOOTSTRAP] open-iscsi installation complete."
