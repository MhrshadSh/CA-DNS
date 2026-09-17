# CA-DNS Roadmap

We build CA-DNS in phases. Each phase ends with a **demo you can run** and
**exit criteria**. We don't start the next phase until the current one passes.
Design rationale lives in [architecture.md](architecture.md).

Status legend: ⬜ not started · 🟨 in progress · ✅ done

---

## Target repository layout

Folders are created when their phase starts. There are no empty placeholder directories.

```
CA-DNS/
├── README.md
├── Makefile                  # make up / down / test / lint / logs / dig
├── compose.yaml              # postgres, resolver, collector, monitor, worker
├── .env.example              # all configuration knobs, no secrets
├── ruff.toml                 # Python lint/format config for the whole repo
├── docs/
│   ├── architecture.md
│   ├── ROADMAP.md
│   └── images/               # architecture diagram, evaluation plots
├── resolver/                 # BIND 9 image + DLZ module
│   ├── Dockerfile
│   ├── config/               # named.conf (+ includes)
│   └── dlz_pgsql/            # C module: src/, include/dlz_minimal.h, Makefile, tests/
├── db/                       # cadns-migrate image: Dockerfile, migrate.sh
│   ├── migrations/           # 0001_init.sql, 0002_dlz_functions.sql, ...
│   └── seed/                 # fixture data for tests/demos
├── services/                 # Python package `cadns` (one image, several entrypoints)
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── src/cadns/
│   │   ├── config.py  db.py  log.py
│   │   ├── queue/            # PostgreSQL-backed FIFO
│   │   ├── collector/        # dnstap → queue ("Aggregate")
│   │   ├── monitor/          # expiry scan, carbon refresh, GC ("IP Monitor")
│   │   ├── worker/           # measurement pipeline
│   │   ├── resolvers/        # upstream pool querying
│   │   ├── geo/              # IPinfo (MMDB + API fallback)
│   │   └── carbon/           # WattTime client
│   └── tests/                # unit tests
├── tests/                    # integration & e2e (uv project + Dockerfile, run in compose)
├── scripts/                  # dev helpers
└── .github/workflows/        # CI
```

Runtime data (IPinfo MMDB, local volumes) goes in `data/` and is gitignored.

---

## Phase 0: Foundation ✅

**Goal:** a clean repo and a working container toolchain.

1. ✅ git repo, `.gitignore`, `.gitattributes`, `.editorconfig`, `.env.example`, `README.md`.
   License still to be decided.
2. **Dev host = the Ubuntu 22.04 arm64 VM** (VMware Fusion). The repo, the tools and
   Docker Engine all live there; there is no Mac-side tooling and no remote docker context.
   - ✅ `scripts/dev/bootstrap-vm.sh` (`sudo scripts/dev/bootstrap-vm.sh`): installs Docker
     Engine and the compose/buildx plugins, adds the user to the `docker` group, sets up log
     rotation, and frees port 53 by disabling the systemd-resolved stub listener. It pins
     host DNS to public resolvers, because VMware Fusion's NAT DNS proxy
     garbles EDNS replies and breaks image pulls.
   - ✅ `scripts/dev/install-tools.sh` (`make tools`): pinned uv, pre-commit (and its git hook)
     and clang-format for the current user, no root needed. dnsutils comes from apt if missing.
   - Config is baked into images where that is production-like; data lives in named volumes.
3. ✅ `compose.yaml` with only `postgres` (PostgreSQL 18, named volume, healthcheck,
   internal `backend` network).
4. ✅ `Makefile`: `tools`, `env`, `up`, `down`, `ps`, `logs`, `psql`, `nuke`, `lint`, `help`.
5. ✅ `.pre-commit-config.yaml`: whitespace/YAML/private-key checks, shellcheck,
   ruff, clang-format. `uv` + `pytest` arrive with the first Python code (Phase 1).

**Exit:** on the VM, `make tools && make lint` passes and `make up && make psql` gives a
PostgreSQL prompt.
✅ Verified 2026-09-14: PostgreSQL 18.6 healthy on the VM (linux/arm64); the `backend`
network has no Internet access; pre-commit hooks pass.
✅ Reworked to a VM-local workflow and re-verified 2026-09-15: `make tools` is idempotent
(uv 0.12.14, pre-commit 4.6.2, clang-format 23.1.1), `make lint` passes, PostgreSQL 18.6 healthy.

---

## Phase 1: Data model & answer policy (SQL) ✅

**Goal:** the database can answer "what is the greenest RRset for X?"

1. ✅ Migration runner (plain ordered `.sql` files applied by a one-shot compose service):
   `db/Dockerfile` + `db/migrate.sh` (`make migrate`, run by `make up`), checksummed,
   one transaction per file; syncs role passwords from `.env` (ADR-6).
2. ✅ `0001_init.sql`: tables from architecture §4, constraints, indexes, the `settings`
   table, roles `cadns_dlz` / `cadns_app`.
3. ✅ `0002_dlz_functions.sql`:
   - `cadns.dlz_findzone(name)`: true only if every stored A/AAAA RRset type has fresh data.
   - `cadns.dlz_lookup(zone, name)` returns `(ttl, type, data)`: greenest-k records
     plus synthesised SOA/NS at `@`.
4. ✅ `db/seed/demo.sql` (`make seed`): a few domains with endpoints in regions of different MOER.
5. ✅ SQL tests (pytest + psycopg, `make test`): ordering, tie-breaking, unknown MOER, expiry,
   TTL clamp, name normalisation, schema constraints, role privileges (ADR-7).

**Exit:** `SELECT * FROM cadns.dlz_lookup('www.example.test','@')` returns the
lowest-MOER seeded address, and tests pass.
✅ Verified 2026-09-15: on the dev DB and on a fresh throwaway project, migrations apply
from empty, the query returns `192.0.2.10` (A) and `2001:db8::10` (AAAA), and 46 tests pass.

---

## Phase 2: Resolver (BIND 9.20 + `dlz_pgsql`) ✅

**Goal:** BIND serves hits from PostgreSQL and recurses on misses.

1. ✅ **Spike (time-boxed):** minimal module that hardcodes one zone. Validate on
   BIND 9.20:
   hit → AA answer; miss → recursion; SOA/NS requirements; behaviour for other
   qtypes (HTTPS/TXT) and child names; dnstap availability in the ISC image.
   Findings in `docs/architecture.md` §5.1. They changed the plan: ISC images are
   amd64-only (→ Debian trixie `bind9`, ADR-1), and children of served names became
   authoritative NXDOMAIN (→ exact-qname matching, ADR-8).
2. ✅ `dlz_pgsql.c`: `dlz_version`, `dlz_create` (module options + libpq parameters,
   credentials from `PG*` env), `dlz_findzonedb` (exact qname, one prepared
   `cadns.dlz_lookup` round trip, rows cached per thread), `dlz_lookup`, `dlz_destroy`;
   thread-safe connection pool; reconnect with backoff; fail-open on errors; logging
   through BIND's `log` callback, once per state change. Unit tests run in the image build.
3. ✅ `resolver/Dockerfile`: multi-stage build on pinned `debian:trixie` (compile against
   `libpq-dev`, run unit tests, copy the `.so` next to the pinned `bind9` package).
4. ✅ `named.conf`: recursion ACLs (loopback + private networks), `dlz "cadns"` block,
   dnstap to `/run/cadns/dnstap.sock`, query logging off, no control channel.
5. ✅ Integration tests (`tests/resolver/`, dnspython): green `aa` answers, recursion,
   children/expired names, database changes, concurrency, fail-open.

**Exit:** `dig @localhost www.example.test` returns the seeded greenest address with `aa`;
`dig @localhost example.com` resolves recursively.
✅ Verified 2026-09-15 on the dev stack and a fresh throwaway project: `192.0.2.10` with `aa`;
`example.com` recursive with `ad`; 56 tests pass. Manual outage test: with PostgreSQL stopped
the resolver answers by recursion (also after a restart), and serves green answers again
once the database is back.

---

## Phase 3: Measurement worker ✅

**Goal:** given a domain, produce a complete measurement in the DB.

1. ✅ `resolvers/`: query A and AAAA in parallel against the configured pool
   (default: 8.8.8.8, 1.1.1.1, 9.9.9.9, 45.90.28.243, 208.67.222.222); follow CNAMEs;
   per-resolver timeout; record the source resolver and TTL.
2. ✅ `geo/`: IPinfo MMDB reader (offline, primary) with API fallback, cached in `endpoints`.
3. ✅ `carbon/`: WattTime v3 client with token refresh (`/login`), `region-from-loc`
   (cached permanently per location, `0003_measurement.sql`), current MOER per region
   (refreshed after 5 min), and rate limiting.
4. ✅ `worker/pipeline.py`: resolve → geolocate new IPs → ensure region MOER → a single
   transaction upsert. Negative results (NXDOMAIN, no addresses) are stored with backoff
   (`domains.retry_after`). Decisions in architecture ADR-9.
5. ✅ CLI: `cadns measure <domain>` (`make measure d=<domain>`) for one-off runs. Unit tests
   use recorded DNS responses and API fixtures, with no network (`make test-services`).

**Exit:** `cadns measure www.youtube.com` populates the DB, and a `dig` through BIND
now returns the greenest endpoint.
✅ Verified 2026-09-16 with real credentials (IPinfo IP-to-Geolocation MMDB + API,
WattTime): `make measure d=www.youtube.com` stores the 16 addresses aggregated from all
5 resolvers, geolocated to CAISO_NORTH at 435 gCO2/kWh, with Google's anycast addresses
ranked unknown; `make measure d=www.un.org` reaches NL at 532 gCO2/kWh and
`dig @localhost www.un.org` returns the lowest-MOER endpoint with `aa`.
55 service tests + 56 integration tests pass.

---

## Phase 4: Queue & collector ("Aggregate") ✅

**Goal:** close the loop automatically: miss → measured → green answers.

1. ✅ `queue/`: `enqueue(domain, reason)` (dedup), `claim(batch)` with `SKIP LOCKED`,
   `complete`, `fail` (exponential backoff, dead letter after N attempts, revived after a
   cooldown), `release`, `LISTEN/NOTIFY` wake-up. Semantics in architecture ADR-3.
2. ✅ Worker service (`cadns worker`): long-running consumer with a concurrency limit and
   graceful shutdown (in-flight jobs finish or are released).
3. ✅ `collector/` (`cadns collector`): dnstap Frame Streams reader on a unix socket shared
   with BIND; classify `CLIENT_RESPONSE` (AA ⇒ hit ⇒ touch `last_queried_at`; non-AA A/AAAA
   ⇒ miss ⇒ enqueue). Batch writes. Ignore special-use and documentation names. Details in
   ADR-2.
4. ✅ End-to-end test (`tests/e2e/`): first `dig` → no `aa`; wait; second `dig` → `aa`,
   greenest IP. Services tests moved to a separate `cadns_test` database (ADR-7).

**Exit:** that end-to-end test passes on a fresh `make up`.
✅ Verified 2026-09-16 on a fresh throwaway project (empty database): 104 services tests and
57 integration tests pass, including the loop test (`www.wikipedia.org` answered `aa` about
4 s after the first miss, measured exactly once). Stable over repeated runs on the dev stack.

---

## Phase 5: IP monitor ✅

**Goal:** keep data fresh without waiting for client misses.

1. ✅ Expiry scan: domains whose RRsets expire within Δ (15 s) **and** were queried within
   the activity window (1 h) are enqueued with `reason = expired` (every 5 s).
2. ✅ Carbon refresh: regions with active endpoints get each new 5-min MOER point (checked
   every minute, fetched only while the current point is missing).
3. ✅ GC: delete domains that have been inactive longer than the retention period (7 days),
   orphaned endpoints and old carbon history (30 days). Short upstream TTLs: configurable
   `cadns.settings.min_record_ttl` (migration 0004, default 0) with a cost table in ADR-10.
4. ✅ Tests with a controllable clock: jobs take `now`, the scheduler takes a fake clock
   (hours simulated in milliseconds). `make test-slow` runs the live freshness test.

**Exit:** a queried domain stays a hit indefinitely while it keeps being queried; carbon
values track WattTime's 5-min updates.
✅ Verified 2026-09-17: `www.un.org` (60 s TTL) queried every 2 s for 3 min got `aa` every
time, re-measured by the monitor every 50 s; NL and CAISO_NORTH signals stored for four
consecutive slots (10:40–10:55), each 16 s after the slot started. 123 services tests,
57 integration tests and the slow e2e test pass.
Open: choose `min_record_ttl` for the evaluation (ADR-10).

---

## Phase 6: Production hardening ⬜

1. Config via env (pydantic-settings), and secrets via Docker secrets, not env files.
2. Healthchecks for every service; `restart: unless-stopped`; resource limits.
3. Structured JSON logs; Prometheus metrics (hit ratio, queue depth/latency,
   API calls & errors, estimated gCO2/kWh saved per answer); optional Grafana dashboard.
4. Security: not an open resolver by default, least-privilege DB roles, non-root containers,
   image scanning.
5. Resilience: DB down ⇒ BIND keeps resolving recursively (fail open);
   WattTime/IPinfo down ⇒ keep last-known values.

---

## Phase 7: Evaluation & CI ⬜

1. GitHub Actions: lint (ruff, clang-format), unit tests, build images, compose
   integration + e2e tests.
2. Performance: `dnsperf`/`resperf` with hit vs. miss mixes; latency added by DLZ.
3. Carbon evaluation against the paper's baseline: random-from-RRset vs.
   CA-DNS answers for the paper's domains (YouTube, Bing, TikTok, UN).
4. Load-distribution metrics (selection amplification) for the chosen policy.

---

## Backlog (post-v1)

- Anycast detection (LACeS census) and traceroute-based location.
- Weighted-random selection policy to reduce endpoint concentration.
- RTT guard (skip green endpoints that are much slower than baseline).
- EDNS Client Subnet forwarding to upstreams.
- Additional carbon providers (Electricity Maps) behind the `carbon/` interface.
