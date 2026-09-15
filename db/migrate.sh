#!/usr/bin/env bash
#
# CA-DNS database migration runner.
#
#   cadns-migrate migrate   apply pending migrations, then sync role passwords
#   cadns-migrate seed      load demo data from the seed directory
#
# Connection settings come from the standard libpq variables (PGHOST, PGUSER,
# PGPASSWORD, PGDATABASE). Migrations run as the database owner.
#
# Each migration file runs in its own transaction together with its row in
# public.schema_migrations, so a failed migration leaves no trace. Applied
# migrations are checksummed; editing one after it was applied is an error.
# Add a new migration instead.
set -Eeuo pipefail

MIGRATIONS_DIR="${MIGRATIONS_DIR:-/cadns/migrations}"
SEED_DIR="${SEED_DIR:-/cadns/seed}"

log() { printf '%s\n' "$*"; }
die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}
psql_() { PGOPTIONS="-c client_min_messages=warning" psql -X -q -v ON_ERROR_STOP=1 "$@"; }

migrate() {
    psql_ -c "CREATE TABLE IF NOT EXISTS public.schema_migrations (
        version    text PRIMARY KEY,
        checksum   text NOT NULL,
        applied_at timestamptz NOT NULL DEFAULT now())"

    local file version checksum applied count=0
    for file in "${MIGRATIONS_DIR}"/*.sql; do
        [[ -e ${file} ]] || die "no migrations found in ${MIGRATIONS_DIR}"
        version="$(basename "${file}" .sql)"
        [[ ${version} =~ ^[0-9]{4}_[a-z0-9_]+$ ]] || die "bad migration file name: ${file}"
        checksum="$(sha256sum "${file}" | cut -d' ' -f1)"

        applied="$(psql_ -At -c "SELECT checksum FROM public.schema_migrations WHERE version = '${version}'")"
        if [[ -n ${applied} ]]; then
            [[ ${applied} == "${checksum}" ]] ||
                die "${version} was modified after it was applied; add a new migration instead"
            continue
        fi

        log "applying ${version}"
        psql_ --single-transaction -f "${file}" \
            -c "INSERT INTO public.schema_migrations (version, checksum) VALUES ('${version}', '${checksum}')"
        count=$((count + 1))
    done
    log "migrations: ${count} applied, schema up to date"

    sync_role_passwords
}

# Roles are created NOLOGIN by the migrations (no secrets in SQL files); the
# passwords come from the environment and are applied on every run.
sync_role_passwords() {
    : "${CADNS_DLZ_PASSWORD:?set CADNS_DLZ_PASSWORD}"
    : "${CADNS_APP_PASSWORD:?set CADNS_APP_PASSWORD}"
    psql_ <<'SQL'
\getenv dlz_password CADNS_DLZ_PASSWORD
\getenv app_password CADNS_APP_PASSWORD
ALTER ROLE cadns_dlz WITH LOGIN PASSWORD :'dlz_password';
ALTER ROLE cadns_app WITH LOGIN PASSWORD :'app_password';
SQL
    log "role passwords synced"
}

seed() {
    local file
    for file in "${SEED_DIR}"/*.sql; do
        [[ -e ${file} ]] || die "no seed files found in ${SEED_DIR}"
        log "seeding $(basename "${file}")"
        psql_ --single-transaction -f "${file}"
    done
}

case "${1:-migrate}" in
migrate) migrate ;;
seed) seed ;;
*) die "usage: cadns-migrate [migrate|seed]" ;;
esac
