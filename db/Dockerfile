# One-shot database migration image: ordered SQL migrations + demo seed data.
ARG POSTGRES_VERSION=18.6
FROM postgres:${POSTGRES_VERSION}-bookworm

COPY --chmod=0755 migrate.sh /usr/local/bin/cadns-migrate
COPY migrations/ /cadns/migrations/
COPY seed/ /cadns/seed/

USER postgres
ENTRYPOINT ["cadns-migrate"]
CMD ["migrate"]
