# Stage 1: Download cookcli binary
FROM alpine:3.21 AS builder

ARG TARGETARCH
ARG COOKCLI_VERSION=0.21.0

RUN apk add --no-cache curl tar && \
    case "${TARGETARCH}" in \
      amd64) ARCH="x86_64" ;; \
      arm64) ARCH="aarch64" ;; \
      *) echo "Unsupported arch: ${TARGETARCH}" && exit 1 ;; \
    esac && \
    curl -fsSL "https://github.com/cooklang/cookcli/releases/download/v${COOKCLI_VERSION}/cook-${ARCH}-unknown-linux-musl.tar.gz" | \
    tar -xz -C /usr/local/bin

# Stage 2: Distroless runtime
FROM gcr.io/distroless/static-debian12:nonroot

LABEL org.opencontainers.image.source="https://github.com/blazepower/rishik-homelab-rke2"
LABEL org.opencontainers.image.description="Cooklang recipe server"
LABEL org.opencontainers.image.licenses="MIT"

COPY --from=builder /usr/local/bin/cook /usr/local/bin/cook

WORKDIR /recipes
EXPOSE 9080

ENTRYPOINT ["/usr/local/bin/cook"]
CMD ["server", "/recipes", "--host", "--port", "9080"]
