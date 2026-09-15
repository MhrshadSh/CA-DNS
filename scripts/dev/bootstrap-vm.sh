#!/usr/bin/env bash
#
# Prepare an Ubuntu 22.04/24.04 VM as the CA-DNS Docker host.
#
# Idempotent: safe to run again. Run on the VM from the repo root, via sudo:
#   sudo scripts/dev/bootstrap-vm.sh            # GROW_ROOT=1 also grows the root volume
#
# What it does:
#   0. (opt-in, GROW_ROOT=1) Grows the root LVM volume into free VG space.
#   1. Installs Docker Engine + buildx + compose plugins. If Ubuntu's docker.io
#      package is already present it is kept and Ubuntu's plugin packages are
#      added; otherwise Docker's official apt repo is used.
#   2. Adds the invoking user to the `docker` group.
#   3. Configures Docker log rotation.
#   4. Frees port 53 by disabling the systemd-resolved stub listener, while
#      keeping the VM's own name resolution working.
set -Eeuo pipefail

GROW_ROOT="${GROW_ROOT:-0}"

if [[ ${EUID} -ne 0 ]]; then
    echo "error: run as root (sudo)" >&2
    exit 1
fi

TARGET_USER="${SUDO_USER:-}"
if [[ -z ${TARGET_USER} || ${TARGET_USER} == root ]]; then
    echo "error: run via sudo from the non-root user that will use Docker" >&2
    exit 1
fi

log() { printf '\n==> %s\n' "$*"; }

export DEBIAN_FRONTEND=noninteractive

# --- 0. Root volume (opt-in) --------------------------------------------------
if [[ ${GROW_ROOT} == 1 ]]; then
    root_dev="$(findmnt -no SOURCE /)"
    if lvs "${root_dev}" >/dev/null 2>&1; then
        vg="$(lvs --noheadings -o vg_name "${root_dev}" | tr -d ' ')"
        free_ext="$(vgs --noheadings -o vg_free_count "${vg}" | tr -d ' ')"
        if ((free_ext > 0)); then
            log "Growing ${root_dev} into free space of volume group ${vg}"
            lvextend --resizefs -l +100%FREE "${root_dev}"
        else
            log "No free space in volume group ${vg}; root volume unchanged"
        fi
    else
        log "Root filesystem is not on LVM; skipping GROW_ROOT"
    fi
    df -h /
fi

# --- 1. Docker Engine ---------------------------------------------------------
if dpkg -s docker.io >/dev/null 2>&1; then
    log "Ubuntu docker.io detected; adding compose and buildx plugins"
    apt-get update -q
    apt-get install -yq docker-compose-v2 docker-buildx
elif ! command -v docker >/dev/null 2>&1; then
    log "Installing Docker Engine"
    apt-get update -q
    apt-get install -yq ca-certificates curl

    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc

    # shellcheck source=/dev/null
    . /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        >/etc/apt/sources.list.d/docker.list

    apt-get update -q
    apt-get install -yq docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
else
    log "Docker already installed: $(docker --version)"
fi

systemctl enable --now docker

# --- 2. docker group ----------------------------------------------------------
if ! id -nG "${TARGET_USER}" | grep -qw docker; then
    log "Adding ${TARGET_USER} to the docker group"
    usermod -aG docker "${TARGET_USER}"
fi

# --- 3. Log rotation ----------------------------------------------------------
DAEMON_JSON=/etc/docker/daemon.json
if [[ ! -f ${DAEMON_JSON} ]]; then
    log "Configuring Docker log rotation"
    cat >"${DAEMON_JSON}" <<'EOF'
{
  "log-driver": "local",
  "log-opts": { "max-size": "20m", "max-file": "5" }
}
EOF
    systemctl restart docker
fi

# --- 4. Host DNS: free port 53, use working upstreams -------------------------
# - systemd-resolved listens on 127.0.0.53:53 and would clash with the resolver
#   container, so its stub listener is disabled.
# - Hypervisor NAT DNS proxies can be broken: VMware Fusion's (x.x.x.2) returns
#   malformed replies to EDNS queries, which breaks Docker image pulls and would
#   leak into containers. /etc/resolv.conf is therefore a static file pointing
#   at public resolvers instead of the DHCP-provided server.
UPSTREAM_DNS="${UPSTREAM_DNS:-1.1.1.1 9.9.9.9}"
RESOLVED_DROPIN=/etc/systemd/resolved.conf.d/10-cadns.conf

want_dropin="$(printf '[Resolve]\nDNSStubListener=no\nDNS=%s\n' "${UPSTREAM_DNS}")"
want_resolv="$(
    printf '# Managed by CA-DNS bootstrap-vm.sh\n'
    for ns in ${UPSTREAM_DNS}; do printf 'nameserver %s\n' "${ns}"; done
    printf 'options edns0 trust-ad\n'
)"

dns_changed=0
install -d /etc/systemd/resolved.conf.d
rm -f /etc/systemd/resolved.conf.d/10-cadns-no-stub.conf # name used by older versions
if [[ "$(cat "${RESOLVED_DROPIN}" 2>/dev/null)" != "${want_dropin}" ]]; then
    printf '%s\n' "${want_dropin}" >"${RESOLVED_DROPIN}"
    dns_changed=1
fi
if [[ -L /etc/resolv.conf || "$(cat /etc/resolv.conf 2>/dev/null)" != "${want_resolv}" ]]; then
    rm -f /etc/resolv.conf
    printf '%s\n' "${want_resolv}" >/etc/resolv.conf
    dns_changed=1
fi
if ((dns_changed)); then
    log "Host DNS: stub listener off, upstreams = ${UPSTREAM_DNS}"
    systemctl restart systemd-resolved
    # Container runtimes cache the previous nameservers; reload them.
    systemctl restart containerd docker
fi

# --- Summary ------------------------------------------------------------------
log "Done"
docker --version
docker compose version
if ss -lntu | grep -qE '[:.]53\s'; then
    echo "warning: something is still listening on port 53:" >&2
    ss -lntup | grep -E '[:.]53\s' >&2
else
    echo "Port 53 is free."
fi
echo "Log out and back in (or run 'newgrp docker') for docker group membership to apply."
