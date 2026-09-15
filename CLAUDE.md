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
  17 GB root disk. Watch disk usage when building images.
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

```bash
make            # list targets
make up         # build + start stack (waits for healthchecks)
make ps | make logs s=<service> | make psql
make down       # stop, keep volumes
make lint       # pre-commit on all tracked files
```

Compose project name is `cadns`; secrets/config come from `.env` (gitignored;
template `.env.example`).

## Pending work

1. **Phase 0 rework (do first):** the repo was set up to be driven from a Mac.
   Convert it to VM-local:
   - Remove the Makefile targets `bootstrap-mac`, `vm-copy-key`, `vm-bootstrap`
     and `context`, and remove `VM_SSH` / `DOCKER_CONTEXT` from `.env.example`, `.env` and the Makefile.
   - Make `scripts/dev/bootstrap-vm.sh` run locally (`sudo scripts/dev/bootstrap-vm.sh`)
     and add a `make` target that installs dev tools on the VM (uv, pre-commit,
     clang-format, dnsutils), replacing `scripts/dev/Brewfile`.
   - Drop the "no bind mounts" rationale in `compose.yaml`. Keep config baked
     into images where that is production-like.
   - Update the README dev-setup section and ROADMAP Phase 0 to match.
2. Phase 1: data model & answer policy (see ROADMAP).
3. Open question: license (not chosen yet; BIND's vendored `dlz_minimal.h` is MPL-2.0).

## Conventions

- **Python:** 3.12+, package `cadns` under `services/`, managed with `uv`; ruff
  for lint/format; pytest.
- **C (DLZ module):** `resolver/dlz_pgsql/`, clang-format; the vendored
  `dlz_minimal.h` stays byte-identical to upstream.
- **SQL:** ordered migrations in `db/migrations/NNNN_name.sql`. The answer policy
  lives in SQL functions (`cadns.dlz_findzone`, `cadns.dlz_lookup`).
- **Shell:** `set -Eeuo pipefail`, idempotent, shellcheck-clean.
- Pin image and tool versions. Verify current versions instead of assuming them.
