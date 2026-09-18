# CA-DNS: Carbon-Aware DNS Resolver

A recursive DNS resolver that steers clients toward lower-carbon service
endpoints. For each domain it aggregates RRsets from multiple upstream public
resolvers, geolocates every endpoint, attaches its grid's Marginal Operational
Emission Rate (MOER), and answers with the greenest address. This is the
*Multi-Resolver Greenest* strategy from *Leveraging DNS for Carbon-Aware Server
Selection* (Shakeri et al., University of Twente).

Built on BIND 9 + PostgreSQL (DLZ), inspired by
[ActiveDNS](https://github.com/MhrshadSh/ActiveDNS).

![Architecture](docs/images/architecture.png)

- Architecture and design decisions: [docs/architecture.md](docs/architecture.md)
- Build plan and status: [docs/ROADMAP.md](docs/ROADMAP.md)

> Status: early development. Phases 0–6 are done: the resolver answers measured names
> with the greenest endpoint, misses are measured automatically (dnstap → queue → worker),
> the monitor keeps active names and carbon signals fresh, and the stack runs with Docker
> secrets, healthchecks, resource limits, JSON logs and Prometheus metrics. Phase 7
> (evaluation & CI) is next.

## Development setup

Everything runs on the Ubuntu 22.04 VM that holds this repository: the code,
the developer tools and the Docker Engine that runs the stack.

```bash
sudo scripts/dev/bootstrap-vm.sh   # once: Docker Engine, log rotation, free port 53, host DNS
make tools                         # uv, pre-commit (+ git hook), clang-format, dig
make env                           # create .env (non-secret settings)
make secrets                       # create ./secrets/* (passwords, API tokens)
make up                            # build, migrate and start the stack
make seed                          # load demo data (*.example.test)
make dig q=www.example.test        # query the resolver on 127.0.0.1:53
make test                          # all tests (services + integration)
make measure d=www.youtube.com     # measure a domain now (needs API credentials in .env)
make psql                          # database shell
```

Run `make` to see all targets.

Measurements need a [WattTime](https://watttime.org) account (MOER) and an
[IPinfo](https://ipinfo.io) token (geolocation): put them in `secrets/watttime_password`
and `secrets/ipinfo_token` (created empty by `make secrets`), and the WattTime username in
`.env`. With IPinfo database access, `make ipinfo-db` downloads the MMDB, used before the API.
