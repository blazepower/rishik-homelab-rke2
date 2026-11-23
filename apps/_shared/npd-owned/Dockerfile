ARG BUILDER
ARG VENDOR
FROM ${BUILDER} AS build
ENV GOTOOLCHAIN=local GOWORK=off GOFLAGS=-mod=readonly GOMAXPROCS=2 \
    GOOS=linux GOARCH=amd64 GOAMD64=v1 CGO_ENABLED=1
RUN apt-get -o Acquire::Retries=0 update && \
    apt-get -o Acquire::Retries=0 install -y --no-install-recommends libsystemd-dev python3
WORKDIR /src
COPY upstream/ ./
COPY source.json build.py maintained_build.py /build/
RUN python3 /build/build.py

FROM ${VENDOR}
RUN --network=none --mount=type=bind,source=runtime-patches,target=/tmp/npd-runtime-patches,ro \
    dpkg -i /tmp/npd-runtime-patches/libapt-pkg6.0.deb /tmp/npd-runtime-patches/apt.deb
COPY --from=build /src/bin/node-problem-detector /node-problem-detector
COPY --from=build /src/bin/health-checker /home/kubernetes/bin/health-checker
COPY --from=build /src/bin/log-counter /home/kubernetes/bin/log-counter
COPY --from=build /build/provenance/ /usr/share/npd-owned/
LABEL org.opencontainers.image.source="https://github.com/blazepower/rishik-homelab-rke2" \
      io.homelab.derivative="Owned supported-minor NPD with Go 1.27.4 and APT 2.6.2; not an official Kubernetes image"
