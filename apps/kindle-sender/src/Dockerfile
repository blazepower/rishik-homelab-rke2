# Multi-stage build for Kindle Sender
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

# Build the application with static linking
RUN CGO_ENABLED=1 GOOS=linux go build -a -ldflags '-linkmode external -extldflags "-static"' -o kindle-sender .

# Final stage - distroless static image
FROM gcr.io/distroless/static-debian12:nonroot

WORKDIR /app

# Copy binary from build stage
COPY --from=build /build/kindle-sender /app/kindle-sender

# Use non-root user (65532 is the nonroot user in distroless)
USER 65532:65532

# Set read-only root filesystem compatible
VOLUME ["/data"]

ENTRYPOINT ["/app/kindle-sender"]
