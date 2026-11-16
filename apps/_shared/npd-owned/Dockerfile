ARG BUILDER
ARG VENDOR
FROM ${BUILDER} AS build
ENV GOTOOLCHAIN=local GOWORK=off GOFLAGS=-mod=readonly GOMAXPROCS=2 \
    GOOS=linux GOARCH=amd64 GOAMD64=v1 CGO_ENABLED=1
RUN apt-get update && apt-get install -y --no-install-recommends libsystemd-dev python3
WORKDIR /src
COPY upstream/ ./
COPY source.json build.py maintained_build.py /build/
RUN python3 /build/build.py

FROM ${VENDOR}
COPY --from=build /src/bin/node-problem-detector /node-problem-detector
COPY --from=build /src/bin/health-checker /home/kubernetes/bin/health-checker
COPY --from=build /src/bin/log-counter /home/kubernetes/bin/log-counter
COPY --from=build /build/provenance/ /usr/share/npd-owned/
LABEL org.opencontainers.image.source="https://github.com/blazepower/rishik-homelab-rke2" \
      io.homelab.derivative="Owned supported-minor NPD rebuild with Go 1.27.4; not an official Kubernetes image"
