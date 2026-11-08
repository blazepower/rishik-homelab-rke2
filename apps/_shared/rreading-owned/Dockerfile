FROM docker.io/blampe/rreading-glasses:hardcover@sha256:3f017a51d9007b715303a20f481c822e4df66485fc9e5f57f6fdf1de840dc02f
COPY --chmod=0755 main /main
COPY source.json /usr/share/rreading-owned/source.json
COPY LICENSE /usr/share/rreading-owned/LICENSE
LABEL org.opencontainers.image.source="https://github.com/blazepower/rishik-homelab-rke2"
LABEL org.opencontainers.image.url="https://github.com/blazepower/rreading-glasses/tree/524c9139707faa3e774b30a13bab688710b02ff2"
LABEL org.opencontainers.image.revision="524c9139707faa3e774b30a13bab688710b02ff2"
LABEL org.opencontainers.image.licenses="GPL-3.0"
LABEL io.blazepower.upstream.revision="ceb4d580f01a920e474ae14d5085ff6809771d22"
LABEL io.blazepower.compiler="go1.27.3"
