"""Schema constraints, migrations and role privileges."""

import os
from pathlib import Path

import psycopg
import pytest

SEED_DIR = Path(__file__).resolve().parents[2] / "db" / "seed"


def test_migrations_are_recorded(cur):
    cur.execute("SELECT version FROM public.schema_migrations ORDER BY version")
    versions = [row[0] for row in cur.fetchall()]

    assert versions[:2] == ["0001_init", "0002_dlz_functions"]


@pytest.mark.parametrize("name", ["WWW.example.test", "www.example.test.", ""])
def test_domain_names_must_be_canonical(cur, name):
    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute("INSERT INTO cadns.domains (name) VALUES (%s)", (name,))


@pytest.mark.parametrize(
    ("rtype", "address"),
    [("A", "fd00::1"), ("AAAA", "10.0.0.1"), ("A", "10.0.0.0/24"), ("MX", "10.0.0.1")],
)
def test_records_must_match_their_type(cur, rtype, address):
    cur.execute("INSERT INTO cadns.domains (name) VALUES ('bad.schema.test')")
    cur.execute("INSERT INTO cadns.endpoints (address) VALUES ('10.0.0.1'), ('fd00::1')")

    with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation)):
        cur.execute(
            """INSERT INTO cadns.rrset_records
                   (domain_id, rtype, address, resolver, ttl, resolved_at, expires_at)
               SELECT id, %s, %s, '8.8.8.8', 60, now(), now() + interval '60 s'
               FROM cadns.domains WHERE name = 'bad.schema.test'""",
            (rtype, address),
        )


def test_settings_has_exactly_one_row(cur):
    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute("INSERT INTO cadns.settings (singleton) VALUES (false)")


def role_conn(role: str) -> psycopg.Connection:
    return psycopg.connect(user=role, password=os.environ[f"{role.upper()}_PASSWORD"])


def test_dlz_role_can_only_execute_dlz_functions():
    with role_conn("cadns_dlz") as conn:
        conn.execute("SELECT cadns.dlz_findzone('www.example.test')")
        conn.execute("SELECT * FROM cadns.dlz_lookup('www.example.test', '@')")

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM cadns.domains")


def test_app_role_can_write_tables():
    with role_conn("cadns_app") as conn:
        conn.execute("INSERT INTO cadns.grid_regions (code) VALUES ('T_APP_ROLE')")
        conn.execute("SELECT * FROM cadns.dlz_lookup('www.example.test', '@')")
        conn.rollback()


def test_demo_seed_answers_greenest_addresses(cur):
    """Phase 1 exit criterion, on the demo seed (loaded twice: it is idempotent)."""
    for _ in range(2):
        for seed in sorted(SEED_DIR.glob("*.sql")):
            cur.execute(seed.read_text())

    cur.execute("SELECT type, data FROM cadns.dlz_lookup('www.example.test', '@')")
    records = cur.fetchall()
    assert ("A", "192.0.2.10") in records
    assert ("AAAA", "2001:db8::10") in records

    cur.execute("SELECT cadns.dlz_findzone('stale.example.test')")
    assert cur.fetchone()[0] is False
