#!/usr/bin/env bash
#
# Install CA-DNS developer tools for the current (non-root) user on the VM.
#
# Idempotent: safe to run again. Run as your normal user:
#   make tools
#
# Installs:
#   - uv            Python toolchain (into ~/.local/bin)
#   - pre-commit    git hooks, installed as a uv tool, plus the repo's hook
#   - clang-format  same version as the pre-commit mirror, for editor use
#   - dnsutils      dig/delv (apt; asks for the sudo password only if missing)
set -Eeuo pipefail

UV_VERSION=0.12.14
PRE_COMMIT_VERSION=4.6.2
CLANG_FORMAT_VERSION=23.1.1 # keep in sync with mirrors-clang-format in .pre-commit-config.yaml

if [[ ${EUID} -eq 0 ]]; then
    echo "error: run as your normal user, not root" >&2
    exit 1
fi

log() { printf '\n==> %s\n' "$*"; }

BIN_DIR="${HOME}/.local/bin"
export PATH="${BIN_DIR}:${PATH}"

# --- uv -----------------------------------------------------------------------
if [[ "$(uv --version 2>/dev/null | awk '{print $2}')" != "${UV_VERSION}" ]]; then
    log "Installing uv ${UV_VERSION}"
    curl -fsSL "https://astral.sh/uv/${UV_VERSION}/install.sh" |
        env UV_NO_MODIFY_PATH=1 UV_INSTALL_DIR="${BIN_DIR}" sh
fi

# --- Python tools (uv tool) ---------------------------------------------------
uv_tool() {
    local pkg=$1 version=$2
    if ! uv tool list 2>/dev/null | grep -qx "${pkg} v${version}"; then
        log "Installing ${pkg} ${version}"
        uv tool install --force "${pkg}==${version}"
    fi
}
uv_tool pre-commit "${PRE_COMMIT_VERSION}"
uv_tool clang-format "${CLANG_FORMAT_VERSION}"

# --- dnsutils (apt) -----------------------------------------------------------
if ! command -v dig >/dev/null 2>&1; then
    log "Installing dnsutils (needs sudo)"
    sudo apt-get update -q
    sudo apt-get install -yq dnsutils
fi

# --- git hooks ----------------------------------------------------------------
repo_root="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
(cd "${repo_root}" && pre-commit install >/dev/null)

# --- Summary ------------------------------------------------------------------
log "Done"
uv --version
pre-commit --version
clang-format --version
dig -v 2>&1
case ":${PATH#"${BIN_DIR}:"}:" in
*":${BIN_DIR}:"*) ;;
*) echo "note: add ${BIN_DIR} to your PATH (Ubuntu's ~/.profile does this on next login)" ;;
esac
