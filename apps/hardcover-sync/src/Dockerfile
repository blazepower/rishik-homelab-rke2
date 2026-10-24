# Multi-stage build for Hardcover Sync
FROM golang:1.27.3-bookworm AS build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libsqlite3-dev \
    ca-certificates \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' ca-certificates)" ge 20250419~deb12u2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy go.mod and go.sum
COPY go.mod ./
COPY go.sum ./

# Download dependencies
RUN go mod download

# Copy source code
COPY main.go ./

# Build the application with static linking
RUN CGO_ENABLED=1 GOOS=linux go build -a -ldflags '-linkmode external -extldflags "-static"' -o hardcover-sync .

COPY --from=gcr.io/distroless/static-debian12:nonroot / /runtime-root/

# Replace hashed trust paths as well as the bundle.
RUN rm -rf /runtime-root/etc/ssl/certs /runtime-root/usr/share/ca-certificates && \
    mkdir -p /runtime-root/etc/ssl /runtime-root/usr/share && \
    cp -a /etc/ssl/certs /runtime-root/etc/ssl/ && \
    cp -a /usr/share/ca-certificates /runtime-root/usr/share/ && \
    cp /etc/ca-certificates.conf /runtime-root/etc/ca-certificates.conf

# Final stage - distroless static image
FROM scratch

COPY --from=build /runtime-root/ /

WORKDIR /app

# Copy binary from build stage
COPY --from=build /build/hardcover-sync /app/hardcover-sync

ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# Use non-root user (65532 is the nonroot user in distroless)
USER 65532:65532

# Set read-only root filesystem compatible
VOLUME ["/data"]

ENTRYPOINT ["/app/hardcover-sync"]
