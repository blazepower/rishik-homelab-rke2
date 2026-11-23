ARG BUILDER
ARG VENDOR
FROM ${BUILDER} AS build
ENV GOTOOLCHAIN=local GOWORK=off GOFLAGS=-mod=readonly GOMAXPROCS=2 \
    GOOS=linux GOARCH=amd64 GOAMD64=v1 CGO_ENABLED=0 \
    KUBECONFIG=/no-production-context
RUN apt-get -o Acquire::Retries=0 update && \
    apt-get -o Acquire::Retries=0 install -y --no-install-recommends python3
WORKDIR /src
COPY upstream/ ./
COPY source.json security.patch build.py maintained_build.py /build/
RUN python3 /build/build.py

FROM ${VENDOR}
ARG COMPONENT
COPY --from=build --chmod=0555 /build/bin/${COMPONENT} /ko-app/${COMPONENT}
COPY --from=build /build/provenance/ /usr/share/http-addon-owned/
LABEL org.opencontainers.image.source="https://github.com/blazepower/rishik-homelab-rke2" \
      io.homelab.derivative="Owned HTTP add-on 0.16.0 with declared Go and dependency security updates; not an official KEDA image"
