# Multi-stage build for Kindle Sender
FROM golang:1.27.3-bookworm@sha256:d9b6a1beb9474adc2a1ce842573639b44b2a0dd49b6669f51d3821baf9ec37c9 AS build

ENV GOTOOLCHAIN=local

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libsqlite3-dev \
    ca-certificates \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' ca-certificates)" ge 20250419~deb12u2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy go.mod and go.sum
COPY go.mod go.sum ./

# Download dependencies
RUN go mod download

# Copy source code
COPY main.go ./
COPY kindle_ca_test.go kindle-ca-spkis.json ca-prepare.go ./
COPY --from=kindle-policy emudhra-2026.json /build/emudhra-2026.json

RUN go build -o /build/kindle-clear-ca ca-prepare.go && \
    /build/kindle-clear-ca /usr/share/ca-certificates /build/emudhra-2026.json && \
    update-ca-certificates --fresh

# Build the application with static linking
RUN CGO_ENABLED=1 GOOS=linux go build -a -ldflags '-linkmode external -extldflags "-static"' -o kindle-sender .
RUN CGO_ENABLED=1 GOOS=linux go test -c -ldflags '-linkmode external -extldflags "-static"' -o /build/kindle-ca.test . && \
    go version -m /build/kindle-sender > /build/kindle-build-info.txt

COPY --from=gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab / /runtime-root/

# Replace every inherited trust path, not just the concatenated bundle.
RUN rm -rf /runtime-root/etc/ssl/certs /runtime-root/usr/share/ca-certificates && \
    mkdir -p /runtime-root/etc/ssl /runtime-root/usr/share && \
    cp -a /etc/ssl/certs /runtime-root/etc/ssl/ && \
    cp -a /usr/share/ca-certificates /runtime-root/usr/share/ && \
    cp /etc/ca-certificates.conf /runtime-root/etc/ca-certificates.conf

FROM scratch AS runtime
COPY --from=build /runtime-root/ /
LABEL org.opencontainers.image.source="https://github.com/blazepower/rishik-homelab-rke2" \
      org.opencontainers.image.version="1.0.0"

WORKDIR /app

# Copy binary from build stage
COPY --from=build /build/kindle-sender /app/kindle-sender
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# Use non-root user (65532 is the nonroot user in distroless)
USER 65532:65532

# Set read-only root filesystem compatible
VOLUME ["/data"]

ENTRYPOINT ["/app/kindle-sender"]

# Tests never invoke main(), load service configuration, or send messages.
FROM runtime AS kindle-ca-acceptance
COPY --from=build /build/kindle-ca.test /proof/kindle-ca.test
COPY --from=build /build/kindle-build-info.txt /proof/kindle-build-info.txt
COPY --from=build /build/emudhra-2026.json /proof/emudhra-2026.json
ENV KINDLE_CA_POLICY=/proof/emudhra-2026.json
ENV KINDLE_CA_RUNTIME=1
ENTRYPOINT ["/proof/kindle-ca.test", "-test.v", "-test.run=^TestKindleCA"]

FROM runtime AS final
