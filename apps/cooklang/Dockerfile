# Stage 1: Download and assemble the runtime using patched system trust.
FROM debian:bookworm-slim AS certificates

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl tar && \
    dpkg --compare-versions "$(dpkg-query -W -f='${Version}' ca-certificates)" ge 20250419~deb12u2 && \
    rm -rf /var/lib/apt/lists/*

ARG TARGETARCH
ARG COOKCLI_VERSION=0.21.0

RUN case "${TARGETARCH}" in \
      amd64) ARCH="x86_64" ;; \
      arm64) ARCH="aarch64" ;; \
      *) echo "Unsupported arch: ${TARGETARCH}" && exit 1 ;; \
    esac && \
    curl -fsSL "https://github.com/cooklang/cookcli/releases/download/v${COOKCLI_VERSION}/cook-${ARCH}-unknown-linux-musl.tar.gz" | \
    tar -xz -C /usr/local/bin

COPY --from=gcr.io/distroless/static-debian12:nonroot / /runtime-root/

# Replace the entire directory so old hashed trust paths cannot survive.
RUN rm -rf /runtime-root/etc/ssl/certs /runtime-root/usr/share/ca-certificates && \
    mkdir -p /runtime-root/etc/ssl /runtime-root/usr/share && \
    cp -a /etc/ssl/certs /runtime-root/etc/ssl/ && \
    cp -a /usr/share/ca-certificates /runtime-root/usr/share/ && \
    cp /etc/ca-certificates.conf /runtime-root/etc/ca-certificates.conf

# Stage 2: Keep the shell-free runtime and its original non-root identity.
FROM scratch

COPY --from=certificates /runtime-root/ /

USER 65532:65532
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

LABEL org.opencontainers.image.source="https://github.com/blazepower/rishik-homelab-rke2"
LABEL org.opencontainers.image.description="Cooklang recipe server"
LABEL org.opencontainers.image.licenses="MIT"

COPY --from=certificates /usr/local/bin/cook /usr/local/bin/cook

WORKDIR /recipes
EXPOSE 9080

ENTRYPOINT ["/usr/local/bin/cook"]
CMD ["server", "/recipes", "--host", "--port", "9080"]
