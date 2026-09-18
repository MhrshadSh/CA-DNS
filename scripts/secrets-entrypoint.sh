#!/usr/bin/env bash
#
# Expand Docker secrets into the environment, then run the service.
#
# For every variable NAME_FILE, read the file and export NAME with its
# contents (trailing newline removed), then unset NAME_FILE. Secrets therefore
# never appear in compose files, `docker inspect`, or the image; they are
# mounted read-only under /run/secrets and read once at start-up.
#
# A plain NAME (no file) still works for local development.
set -Eeuo pipefail

while IFS='=' read -r file_var path; do
    var="${file_var%_FILE}"
    if [[ -z ${path} ]]; then
        # Explicitly empty: fall back to the plain variable (handy for one-off runs).
        unset "${file_var}"
        continue
    fi
    if [[ ! -r ${path} ]]; then
        echo "entrypoint: ${file_var} points at ${path}, which cannot be read" >&2
        exit 1
    fi
    value="$(cat "${path}")"
    export "${var}=${value}"
    unset "${file_var}"
done < <(env | grep -E '^[A-Za-z_][A-Za-z0-9_]*_FILE=' || true)

exec "$@"
