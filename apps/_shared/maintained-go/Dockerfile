ARG BUILDER
ARG VENDOR
FROM ${BUILDER} AS build
ENV GOTOOLCHAIN=local GOOS=linux GOARCH=amd64 GOAMD64=v1 \
    GOTMPDIR=/build/go-work TMPDIR=/build/scratch
RUN apk add --no-cache git make build-base python3
RUN mkdir -p /build/go-work /build/scratch
WORKDIR /src
COPY upstream/ ./
COPY build.py settings.json /build/
RUN python3 /build/build.py

FROM ${VENDOR} AS runtime
COPY --from=build /build/application /maintained-application
ARG BINARY
RUN test -f "${BINARY}" && cp /maintained-application "${BINARY}" && rm /maintained-application
COPY --from=build /build/provenance/ /usr/share/maintained-go/
LABEL org.opencontainers.image.source="https://github.com/blazepower/rishik-homelab-rke2" \
      io.homelab.derivative="Same application version, patched Go compiler; not an official vendor build"
