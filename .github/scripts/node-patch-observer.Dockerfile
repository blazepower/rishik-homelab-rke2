FROM docker.io/library/ubuntu@sha256:0d5255be560e2d2ddcfbfda88549e9e815ef29e7cd7fc62183d5709e6e2963c6

# Public, isolated Ubuntu 26.04 protocol-test SDK; retained indexes are test inputs.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       python3 python3-apt systemd
