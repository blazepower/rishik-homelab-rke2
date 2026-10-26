#!/bin/bash
set -Eeuo pipefail
umask 022
trap 'printf "Plex CA hardening failed at line %s; refusing startup.\n" "$LINENO" >&2' ERR

die() {
  printf 'Plex CA hardening: %s; refusing startup.\n' "$*" >&2
  exit 1
}

root="${1:-/}"
root="$(cd "$root" && pwd -P)"
root="${root%/}"
policy="$(dirname "$0")/blocked-spkis.sha256"
declare -A denied=()
while IFS= read -r digest || [[ -n "$digest" ]]; do
  [[ "$digest" =~ ^[A-F0-9]{64}$ ]] || die "invalid policy digest"
  [[ ! ${denied[$digest]+present} ]] || die "duplicate policy digest"
  denied["$digest"]=1
done < "$policy"
[[ ${#denied[@]} -eq 11 ]] || die "expected all eleven policy keys"

sources=("$root/usr/share/ca-certificates" "$root/usr/local/share/ca-certificates")
certdir="$root/etc/ssl/certs"
bundle="$certdir/ca-certificates.crt"
plex="$root/usr/lib/plexmediaserver/Resources/cacert.pem"
for directory in "${sources[@]}" "$certdir"; do
  [[ -d "$directory" && ! -L "$directory" ]] || die "missing or symlinked trust directory: $directory"
done
for file in "$bundle" "$plex" "$root/etc/ca-certificates.conf"; do
  [[ -f "$file" && ! -L "$file" ]] || die "missing or symlinked trust input: $file"
done

work="$(mktemp -d /tmp/plex-ca-hardening.XXXXXX)"
trap 'rm -f -- "$work/certificate.pem" "$work/output.pem" "$work/files" "$work/symlinks"; rmdir -- "$work"' EXIT
find "${sources[@]}" -type l -print -quit > "$work/symlinks"
[[ ! -s "$work/symlinks" ]] || die "unsupported symlink in source trust directories"

filter_bundle() {
  local input="$1" line inside=0 digest
  total=0
  kept=0
  removed=0
  : > "$work/output.pem"
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    if [[ "$line" == "-----BEGIN CERTIFICATE-----" ]]; then
      [[ "$inside" -eq 0 ]] || die "nested PEM block in $input"
      inside=1
      printf '%s\n' "$line" > "$work/certificate.pem"
    elif [[ "$line" == "-----END CERTIFICATE-----" ]]; then
      [[ "$inside" -eq 1 ]] || die "unexpected PEM terminator in $input"
      printf '%s\n' "$line" >> "$work/certificate.pem"
      digest="$(openssl x509 -in "$work/certificate.pem" -pubkey -noout |
        openssl pkey -pubin -outform DER | openssl dgst -sha256 -r)"
      digest="${digest%% *}"
      digest="${digest^^}"
      [[ "$digest" =~ ^[A-F0-9]{64}$ ]] || die "could not hash certificate in $input"
      ((total+=1))
      if [[ ${denied[$digest]+present} ]]; then
        ((removed+=1))
      else
        cat "$work/certificate.pem" >> "$work/output.pem"
        ((kept+=1))
      fi
      inside=0
    elif [[ "$line" == -----BEGIN* || "$line" == -----END* ]]; then
      die "unsupported PEM block in $input"
    elif [[ "$inside" -eq 1 ]]; then
      printf '%s\n' "$line" >> "$work/certificate.pem"
    fi
  done < "$input"
  [[ "$inside" -eq 0 && "$total" -gt 0 ]] || die "empty or malformed certificate file: $input"
}

replace_filtered() {
  local file="$1"
  if [[ "$removed" -eq 0 ]]; then
    return
  fi
  printf 'Removed %s denied certificate(s) from %s\n' "$removed" "$file"
  if [[ "$kept" -eq 0 ]]; then
    rm -- "$file"
  else
    chmod --reference="$file" "$work/output.pem"
    chown --reference="$file" "$work/output.pem"
    mv -- "$work/output.pem" "$file"
  fi
}

find "${sources[@]}" -type f -name '*.crt' -print0 > "$work/files"
while IFS= read -r -d '' file; do
  filter_bundle "$file"
  replace_filtered "$file"
done < "$work/files"

# Preserve unrelated standalone CA files, but remove alternate default CA-directory paths.
find "$certdir" -maxdepth 1 -regextype posix-extended -type f \
  \( -name '*.pem' -o -name '*.crt' -o -regex '.*/[0-9a-f]{8}\.[0-9]+' \) \
  ! -name 'ca-certificates.crt' -print0 > "$work/files"
while IFS= read -r -d '' file; do
  filter_bundle "$file"
  replace_filtered "$file"
done < "$work/files"

filter_bundle "$plex"
[[ "$kept" -ge 10 ]] || die "Plex bundle would be unexpectedly small"
replace_filtered "$plex"

# Rebuild the bundle and hashed links from clean sources without unrelated update hooks.
update-ca-certificates --fresh --certsconf "$root/etc/ca-certificates.conf" \
  --certsdir "${sources[0]}" --localcertsdir "${sources[1]}" \
  --etccertsdir "$certdir" --hooksdir "$work/no-hooks"

find -L "$certdir" -maxdepth 1 -regextype posix-extended -type f \
  \( -name '*.pem' -o -name '*.crt' -o -regex '.*/[0-9a-f]{8}\.[0-9]+' \) \
  -print0 > "$work/files"
while IFS= read -r -d '' file; do
  filter_bundle "$file"
  [[ "$removed" -eq 0 ]] || die "denied key remains in $file"
done < "$work/files"
for file in "$bundle" "$plex"; do
  filter_bundle "$file"
  [[ "$removed" -eq 0 && "$kept" -ge 10 ]] || die "trust-bundle verification failed: $file"
  printf 'Verified %s: %s certificates, zero denied keys\n' "$file" "$kept"
done
printf 'Plex CA hardening complete; original /init may start.\n'
