# Test runner: pytest + psycopg on the compose backend network.
# Build context is the repository root (tests read db/seed).
FROM python:3.14.7-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.14 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /cadns/tests
COPY tests/pyproject.toml tests/uv.lock ./
RUN uv sync --locked --no-install-project --no-cache

COPY db/seed/ /cadns/db/seed/
COPY tests/ ./

RUN useradd --system --no-create-home tester
USER tester
ENTRYPOINT ["pytest"]
