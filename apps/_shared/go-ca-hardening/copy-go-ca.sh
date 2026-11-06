#!/bin/sh
set -eu

case "$1" in
    stash) packages="usr/lib/python3.12/site-packages/pip/_vendor/certifi" ;;
    syncthing) packages="" ;;
    *) echo "Unsupported public trust profile" >&2; exit 1 ;;
esac
test "$(readlink /etc/ssl/cert.pem)" = "certs/ca-certificates.crt"
for tree in etc/ssl usr/share/ca-certificates usr/local/share/ca-certificates $packages; do
    test -d "/$tree"
    test ! -L "/$tree"
    mkdir -p "/ca-store/$tree"
    cp -a "/$tree/." "/ca-store/$tree/"
done
