# CA-DNS resolver: BIND 9.20 (Debian package) + the dlz_pgsql module.
#
# Debian's bind9 package is used because ISC's images are amd64-only
# (docs/architecture.md ADR-1). Package versions are pinned; when Debian
# publishes a security update the old version leaves the archive and the build
# fails, which is the cue to bump them (apt-cache policy bind9 libpq5).
ARG DEBIAN_IMAGE=debian:trixie-20260824-slim

FROM ${DEBIAN_IMAGE} AS build
ARG LIBPQ_VERSION=17.11-0+deb13u1
RUN apt-get update -q \
 && apt-get install -yq --no-install-recommends gcc libc6-dev make pkgconf "libpq-dev=${LIBPQ_VERSION}" \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY dlz_pgsql/ ./
RUN make test all

FROM ${DEBIAN_IMAGE}
ARG BIND9_VERSION=1:9.20.26-1~deb13u1
ARG LIBPQ_VERSION=17.11-0+deb13u1
RUN apt-get update -q \
 && apt-get install -yq --no-install-recommends \
        "bind9=${BIND9_VERSION}" "bind9-dnsutils=${BIND9_VERSION}" "libpq5=${LIBPQ_VERSION}" \
 && rm -rf /var/lib/apt/lists/* /etc/bind/rndc.key \
 && install -d -o bind -g bind -m 0750 /run/named \
 && install -d -o 10001 -g 10001 -m 0755 /run/cadns

COPY --from=build /src/build/dlz_pgsql.so /usr/lib/cadns/dlz_pgsql.so
COPY config/named.conf /etc/bind/named.conf

EXPOSE 53/udp 53/tcp
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD dig @127.0.0.1 +norecurse +time=2 +tries=1 . NS >/dev/null || exit 1

# -4: the default Docker network has no IPv6; trying IPv6 root servers slows
# cold-cache priming (docs/architecture.md §5.1 #13).
ENTRYPOINT ["/usr/sbin/named", "-f", "-u", "bind", "-c", "/etc/bind/named.conf"]
CMD ["-4"]
