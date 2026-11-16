ARG BUILDER
ARG VENDOR
FROM ${BUILDER} AS build
ENV GOTOOLCHAIN=local GOWORK=off CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOAMD64=v1 \
    GOMAXPROCS=2 GOPROXY=https://proxy.golang.org GOSUMDB=sum.golang.org
RUN apk add --no-cache git bash gzip python3
WORKDIR /src
COPY upstream/ ./
COPY source.json security.patch ui-files.json build.py maintained_build.py /build/
RUN python3 /build/build.py

FROM ${VENDOR}
COPY --from=build /build/bin/prometheus /usr/bin/prometheus
COPY --from=build /build/bin/promtool /usr/bin/promtool
COPY --from=build /build/provenance/ /usr/share/prometheus-owned/
LABEL org.opencontainers.image.source="https://github.com/blazepower/rishik-homelab-rke2" \
      io.homelab.derivative="Same-version Prometheus with Go 1.27.4 and locked module updates; not an official Prometheus image"
