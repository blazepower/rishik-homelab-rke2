#!/bin/sh
set -eu
test "$#" -eq 1
case "$1" in
  bazarr) packages="/app/bazarr/bin/libs/certifi /lsiopy/lib/python3.12/site-packages/pip/_vendor/certifi" ;;
  prowlarr) packages="" ;;
  sabnzbd) packages="/lsiopy/lib/python3.14/site-packages/certifi /lsiopy/lib/python3.14/site-packages/pip/_vendor/certifi" ;;
  bookshelf|qbittorrent|gluetun|overseerr) packages="" ;;
  *) exit 1 ;;
esac
test -d /ca-store
test -z "$(ls -A /ca-store)"
case "$1" in
  bookshelf|qbittorrent|gluetun|overseerr)
    test -d /etc/ssl
    test ! -L /etc/ssl
    test -d /etc/ssl/certs
    test ! -L /etc/ssl/certs
    test -f /etc/ssl/certs/ca-certificates.crt
    test ! -L /etc/ssl/certs/ca-certificates.crt
    test -L /etc/ssl/cert.pem
    test "$(readlink /etc/ssl/cert.pem)" = "certs/ca-certificates.crt"
    directories="/etc/ssl"
    if test "$1" = overseerr; then
      test ! -e /usr/share/ca-certificates
      test ! -L /usr/share/ca-certificates
      test ! -e /usr/local/share/ca-certificates
      test ! -L /usr/local/share/ca-certificates
    else
      directories="$directories /usr/share/ca-certificates /usr/local/share/ca-certificates"
    fi
    for directory in $directories; do
      test -d "$directory"
      test ! -L "$directory"
      mkdir -p "/ca-store$(dirname "$directory")"
      cp -a "$directory" "/ca-store$directory"
    done
    exit 0
    ;;
esac
for directory in /etc/ssl/certs /usr/share/ca-certificates; do
  test -d "$directory"
  test ! -L "$directory"
  mkdir -p "/ca-store$(dirname "$directory")"
  cp -a "$directory" "/ca-store$directory"
done
directory=/usr/local/share/ca-certificates
mkdir -p "/ca-store$(dirname "$directory")"
if test -e "$directory"; then
  test -d "$directory"
  test ! -L "$directory"
  cp -a "$directory" "/ca-store$directory"
else
  mkdir "/ca-store$directory"
fi
for directory in $packages; do
  test -d "$directory"
  test ! -L "$directory"
  test -f "$directory/cacert.pem"
  test ! -L "$directory/cacert.pem"
  mkdir -p "/ca-store$(dirname "$directory")"
  cp -a "$directory" "/ca-store$directory"
done
