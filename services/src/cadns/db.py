"""PostgreSQL access. Connection settings come from libpq's PG* variables."""

import psycopg


async def connect(**kwargs: object) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(application_name="cadns", **kwargs)
