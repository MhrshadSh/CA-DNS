# One-shot database migration image: ordered SQL migrations + demo seed data.
ARG POSTGRES_VERSION=18.6
FROM postgres:${POSTGRES_VERSION}-bookworm

COPY --chmod=0755 db/migrate.sh /usr/local/bin/cadns-migrate
COPY --chmod=0755 scripts/secrets-entrypoint.sh /usr/local/bin/cadns-entrypoint
COPY db/migrations/ /cadns/migrations/
COPY db/seed/ /cadns/seed/

USER postgres
ENTRYPOINT ["cadns-entrypoint", "cadns-migrate"]
CMD ["migrate"]
