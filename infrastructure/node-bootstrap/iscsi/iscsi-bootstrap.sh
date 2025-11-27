#!/bin/bash
set -e

echo "[RKE2 BOOTSTRAP] Installing open-iscsi..."

if ! dpkg -s open-iscsi >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y open-iscsi
fi

ln -sf /usr/bin/iscsiadm /usr/sbin/iscsiadm
ln -sf /usr/bin/iscsiadm /sbin/iscsiadm

systemctl enable iscsid
systemctl start iscsid

echo "[RKE2 BOOTSTRAP] open-iscsi installation complete."
