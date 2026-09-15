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

> Status: early development. Phase 0 (foundation) is done; Phase 1 (data model) is next.

## Development setup

Everything runs on the Ubuntu 22.04 VM that holds this repository: the code,
the developer tools and the Docker Engine that runs the stack.

```bash
sudo scripts/dev/bootstrap-vm.sh   # once: Docker Engine, log rotation, free port 53, host DNS
make tools                         # uv, pre-commit (+ git hook), clang-format, dig
make env                           # create .env with a generated DB password
make up                            # build and start the stack
make psql                          # database shell
```

Run `make` to see all targets.
