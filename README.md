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

Code is edited on the Mac; containers run on a Docker Engine inside an Ubuntu
VM, driven through a remote docker context over SSH.

```bash
make bootstrap-mac   # docker CLI, uv, pre-commit, clang-format, dig
make env             # create .env (then set VM_SSH=user@vm-ip)
make vm-copy-key     # key-based SSH to the VM
make vm-bootstrap    # install Docker on the VM, free port 53
make context         # create the 'cadns-vm' docker context
make up              # start the stack
make psql            # database shell
```

Run `make` to see all targets.
