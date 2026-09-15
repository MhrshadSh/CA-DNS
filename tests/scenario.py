"""Helper for building answer-policy scenarios (domains, endpoints, regions, records)."""

from dataclasses import dataclass

import psycopg
from psycopg import sql


@dataclass
class Scenario:
    """Inserts domains, endpoints, regions and records; calls the DLZ functions."""

    cur: psycopg.Cursor

    def settings(self, **values: object) -> None:
        for column, value in values.items():
            self.cur.execute(
                sql.SQL("UPDATE cadns.settings SET {} = %s").format(sql.Identifier(column)),
                (value,),
            )

    def region(self, code: str, moer: float, age: float = 0) -> None:
        """Region with a MOER point `age` seconds old (older points are history)."""
        self.cur.execute(
            "INSERT INTO cadns.grid_regions (code) VALUES (%s) ON CONFLICT DO NOTHING", (code,)
        )
        self.cur.execute(
            """INSERT INTO cadns.carbon_signals (region_code, point_time, moer_g_per_kwh)
               VALUES (%s, now() - make_interval(secs => %s), %s)""",
            (code, age, moer),
        )

    def endpoint(self, address: str, region: str | None = None, anycast: bool = False) -> None:
        self.cur.execute(
            """INSERT INTO cadns.endpoints (address, region_code, is_anycast)
               VALUES (%s, %s, %s)
               ON CONFLICT (address) DO UPDATE
               SET region_code = EXCLUDED.region_code, is_anycast = EXCLUDED.is_anycast""",
            (address, region, anycast),
        )

    def record(
        self,
        domain: str,
        address: str,
        *,
        rtype: str | None = None,
        resolver: str = "8.8.8.8",
        ttl: int = 3600,
        age: float = 0,
    ) -> None:
        """RRset record resolved `age` seconds ago; creates domain/endpoint if missing."""
        rtype = rtype or ("AAAA" if ":" in address else "A")
        self.cur.execute(
            "INSERT INTO cadns.domains (name, status) VALUES (%s, 'resolved') "
            "ON CONFLICT (name) DO NOTHING",
            (domain,),
        )
        self.cur.execute(
            "INSERT INTO cadns.endpoints (address) VALUES (%s) ON CONFLICT DO NOTHING", (address,)
        )
        self.cur.execute(
            """INSERT INTO cadns.rrset_records
                   (domain_id, rtype, address, resolver, ttl, resolved_at, expires_at)
               SELECT d.id, %(rtype)s, %(address)s, %(resolver)s, %(ttl)s, t.resolved_at,
                      t.resolved_at + make_interval(secs => %(ttl)s)
               FROM cadns.domains d,
                    LATERAL (SELECT now() - make_interval(secs => %(age)s) AS resolved_at) t
               WHERE d.name = %(domain)s""",
            {
                "domain": domain,
                "rtype": rtype,
                "address": address,
                "resolver": resolver,
                "ttl": ttl,
                "age": age,
            },
        )

    def findzone(self, name: str) -> bool:
        self.cur.execute("SELECT cadns.dlz_findzone(%s)", (name,))
        return self.cur.fetchone()[0]

    def lookup(self, zone: str, name: str = "@") -> list[tuple[int, str, str]]:
        self.cur.execute("SELECT ttl, type, data FROM cadns.dlz_lookup(%s, %s)", (zone, name))
        return self.cur.fetchall()

    def answers(self, zone: str, rtype: str = "A") -> list[str]:
        """Addresses of one type, in answer order."""
        return [data for _, type_, data in self.lookup(zone) if type_ == rtype]

    def ttls(self, zone: str) -> dict[str, int]:
        return {type_: ttl for ttl, type_, _ in self.lookup(zone)}
