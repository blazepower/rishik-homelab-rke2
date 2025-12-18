# Multi-stage build for Calibre Converter
FROM golang:1.21-bookworm AS build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy go.mod and go.sum
COPY go.mod go.sum ./

# Download dependencies
RUN go mod download

# Copy source code
COPY main.go ./

# Build the application with CGO enabled for SQLite
RUN CGO_ENABLED=1 GOOS=linux go build -a -ldflags '-extldflags "-static"' -o calibre-converter .

# Final stage - Debian with Calibre
FROM debian:bookworm-slim

# Install calibre and required dependencies
# Pin calibre version where possible via apt-cache policy
RUN apt-get update && apt-get install -y --no-install-recommends \
    calibre=6.13.0+repack-2+deb12u1 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user
RUN groupadd -g 1000 converter && \
    useradd -u 1000 -g converter -m -s /bin/bash converter

WORKDIR /app

# Copy binary from build stage
COPY --from=build /build/calibre-converter /app/calibre-converter

# Create directories for data (tmp is mounted as emptyDir in Kubernetes)
RUN mkdir -p /data && \
    chown -R converter:converter /app /data

# Use non-root user
USER 1000:1000

# Set read-only root filesystem compatible
VOLUME ["/data", "/media"]

ENTRYPOINT ["/app/calibre-converter"]
