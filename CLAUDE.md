# CLAUDE.md

Guidance for Claude Code sessions working on this repository.

## Project

CA-DNS is a carbon-aware recursive DNS resolver that implements the paper's
*Multi-Resolver Greenest* strategy. It aggregates A/AAAA RRsets from several
public resolvers, attaches WattTime MOER via IPinfo geolocation, and answers
with the lowest-MOER endpoint. It is built on BIND 9 + PostgreSQL DLZ,
inspired by [ActiveDNS](https://github.com/MhrshadSh/ActiveDNS). The user is
the paper's author.

- **Design and decisions (ADRs, schema, risks):** `docs/architecture.md`. Read it
  before changing the design. Record new decisions and spike findings there.
- **Plan and status:** `docs/ROADMAP.md`.

## How the user wants to work

- Build **phase by phase** following `docs/ROADMAP.md`. Each phase ends with a
  runnable demo and exit criteria. Verify them, then mark the phase ✅ (with the
  date) before starting the next. Don't jump ahead.
- Guide step by step: explain what is being built and why, briefly.
- Keep the repo clean and professional. Create folders only when their phase
  needs them (no empty placeholder dirs), and keep the layout from ROADMAP.
- **Everything runs on the Ubuntu VM** where this repo lives. Do not introduce
  Mac-side tooling or remote docker contexts / SSH-driven workflows.
- Commit only when asked.

## Environment (dev VM)

- Ubuntu 22.04.5 arm64 (`compute1`) under VMware Fusion; 2 CPUs, 3.8 GiB RAM,
  76 GB root disk (LVM, grown 2026-09-16).
- **sudo requires a password**: give the user the exact command to run
  instead of trying it.
- Docker: Ubuntu's `docker.io` 29.x with `docker-compose-v2` and `docker-buildx`
  packages; log rotation in `/etc/docker/daemon.json`.
- **VMware NAT DNS (`172.16.205.2`) returns malformed replies to EDNS queries**
  and breaks Docker pulls and Go/BIND resolvers. The host `/etc/resolv.conf` is a
  static file pointing at `1.1.1.1` / `9.9.9.9` (set by `scripts/dev/bootstrap-vm.sh`).
  Never test the resolver against the VMware DNS proxy. Direct UDP/53 to the
  Internet works.
- systemd-resolved's stub listener is disabled, so port 53 is free for the resolver container.
- The VM also holds the user's **ActiveDNS** images, container and the
  `activedns-postgres` volume. Never remove them; avoid `docker system prune`.

## Commands

One-time host setup: `sudo scripts/dev/bootstrap-vm.sh`. Tools land in
`~/.local/bin`, so make sure it is on `PATH`.

```bash
make            # list targets
make tools      # dev tools for your user (uv, pre-commit, clang-format, dig)
make secrets    # create ./secrets/* (passwords, API tokens); required before make up
make up         # build, run migrations, start stack (waits for healthchecks)
make migrate | make seed   # apply migrations | load db/seed demo data
make test       # all tests: test-services (unit + DB) and test-integration (pytest args: a="-k ttl")
make test-slow  # minutes-long e2e: an active domain stays served across TTLs (monitor)
make measure d=www.youtube.com   # one-off measurement (cli service, needs API creds in .env)
make ipinfo-db  # download IPinfo MMDB into data/ipinfo (IPINFO_DB=ipinfo_location|ipinfo_core)
make dig q="www.example.test AAAA"   # query the resolver (127.0.0.1:53 by default)
make ps | make logs s=<service> | make psql
make down       # stop, keep volumes
make lint       # pre-commit on all tracked files
```

Compose project name is `cadns`. Non-secret config is in `.env` (template `.env.example`);
credentials are files in `./secrets` (Docker secrets, `make secrets`) — never put them in
`.env` or `compose.yaml` (ADR-11). Services log JSON and expose Prometheus metrics on :9100.

## Pending work

1. Decide `cadns.settings.min_record_ttl` (short CDN TTLs; cost table in ADR-10), then Phase 7:
   evaluation & CI — see ROADMAP.
2. Open question: license (not chosen yet; the vendored `dlz_minimal.h` is ISC-licensed).

## Conventions

- **Python:** 3.12+, package `cadns` under `services/`, managed with `uv`; ruff
  for lint/format (root `ruff.toml`); pytest + pytest-asyncio. One image (`services/Dockerfile`,
  targets `runtime` and `test`), entrypoint `cadns <command>`. DB settings via libpq `PG*`
  env, app settings via `CADNS_*` (pydantic-settings). Unit tests never touch the network:
  recorded DNS wire fixtures and `httpx.MockTransport`; DB tests are rolled-back transactions
  as `cadns_app`. Generate `services/uv.lock` inside the pinned Python image.
- **C (DLZ module):** `resolver/dlz_pgsql/`, clang-format (`resolver/dlz_pgsql/.clang-format`);
  the vendored `dlz_minimal.h` (ISC dlz-modules commit `068c1e5`) stays byte-identical.
  Keep BIND-independent logic in `src/util.c` with unit tests in `tests/` (run by the image build).
  The module serves exact qnames only and never queries the DB in `dlz_lookup` (ADR-8).
- **Resolver image:** Debian trixie `bind9` pinned by package version (ISC images are amd64-only).
- **SQL:** ordered migrations in `db/migrations/NNNN_name.sql`; never edit an applied
  one (checksummed), add a new migration. The answer policy lives in SQL functions
  (`cadns.dlz_findzone`, `cadns.dlz_lookup`).
- **Tests:** integration tests are a separate uv project in `tests/`, run in the `tests`
  compose service (Postgres has no host port) against the dev DB; each test is a rolled-back
  transaction, except resolver/e2e tests that must commit (they clean up). Services tests use
  the separate `cadns_test` database, because the live worker consumes the dev queue.
  Test names under `.test` and `example.com` are never measured (collector ignore list).
  Generate `tests/uv.lock` inside the pinned Python image (host Python is 3.10).
- **Shell:** `set -Eeuo pipefail`, idempotent, shellcheck-clean.
- Pin image and tool versions. Verify current versions instead of assuming them.
