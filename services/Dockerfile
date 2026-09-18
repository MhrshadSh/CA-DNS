# CA-DNS Python services: one image, several entrypoints (cadns <command>).
# Targets: runtime (default) and test (unit + database tests).
FROM python:3.14.7-slim-bookworm AS base

COPY --from=ghcr.io/astral-sh/uv:0.12.14 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /cadns/services
COPY services/pyproject.toml services/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY services/src/ src/
# Fixed uid/gid: /run/cadns (the dnstap socket volume) is owned by it in the
# resolver image too, whichever container initialises the volume.
RUN uv sync --locked --no-dev --no-editable \
 && groupadd --system --gid 10001 cadns \
 && useradd --system --no-create-home --uid 10001 --gid 10001 cadns \
 && install -d -o 10001 -g 10001 -m 0755 /run/cadns

FROM base AS test
RUN uv sync --locked --no-editable
COPY services/tests/ tests/
COPY --chmod=0755 scripts/secrets-entrypoint.sh /usr/local/bin/cadns-entrypoint
USER cadns
ENTRYPOINT ["cadns-entrypoint", "pytest"]

FROM base AS runtime
COPY --chmod=0755 scripts/secrets-entrypoint.sh /usr/local/bin/cadns-entrypoint
USER cadns
ENTRYPOINT ["cadns-entrypoint", "cadns"]
