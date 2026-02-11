# Multi-stage build for Hardcover Sync
FROM golang:1.26-bookworm AS build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libsqlite3-dev \
    ca-certificates \
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

# Final stage - distroless static image
FROM gcr.io/distroless/static-debian12:nonroot

WORKDIR /app

# Copy binary from build stage
COPY --from=build /build/hardcover-sync /app/hardcover-sync

# Copy CA certificates for HTTPS requests
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/

# Use non-root user (65532 is the nonroot user in distroless)
USER 65532:65532

# Set read-only root filesystem compatible
VOLUME ["/data"]

ENTRYPOINT ["/app/hardcover-sync"]
