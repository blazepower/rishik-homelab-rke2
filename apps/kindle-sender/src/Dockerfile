# Multi-stage build for Kindle Sender
FROM golang:1.21-alpine AS builder

# Install build dependencies
RUN apk add --no-cache gcc musl-dev sqlite-dev

WORKDIR /build

# Copy go.mod and go.sum
COPY go.mod ./

# Download dependencies
RUN go mod download

# Copy source code
COPY main.go ./

# Build the application
RUN CGO_ENABLED=1 GOOS=linux go build -a -ldflags '-extldflags "-static"' -o kindle-sender .

# Final stage
FROM alpine:3.19

# Install runtime dependencies
RUN apk add --no-cache ca-certificates tzdata

# Create non-root user
RUN addgroup -g 1000 appuser && \
    adduser -D -u 1000 -G appuser appuser

# Create directories
RUN mkdir -p /data /media && \
    chown -R appuser:appuser /data

WORKDIR /app

# Copy binary from builder
COPY --from=builder /build/kindle-sender /app/kindle-sender

# Use non-root user
USER 1000

# Set read-only root filesystem compatible
VOLUME ["/data"]

ENTRYPOINT ["/app/kindle-sender"]
