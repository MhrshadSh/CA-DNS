"""Shared fixtures for tests that run against the compose stack.

Connection settings come from the libpq environment (PGHOST, PGUSER, ...),
set by the `tests` service in compose.yaml.
"""

from collections.abc import Iterator

import psycopg
import pytest


@pytest.fixture(scope="session")
def owner_conn() -> Iterator[psycopg.Connection]:
    """Connection as the database owner, shared across the session."""
    with psycopg.connect() as conn:
        yield conn


@pytest.fixture
def cur(owner_conn: psycopg.Connection) -> Iterator[psycopg.Cursor]:
    """Cursor inside a transaction that is rolled back after the test.

    Everything a test inserts disappears afterwards, and now() is fixed for the
    whole test, which makes TTL arithmetic exact.
    """
    with owner_conn.cursor() as cursor:
        yield cursor
    owner_conn.rollback()
