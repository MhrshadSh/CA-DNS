# CA-DNS: Carbon-Aware DNS Resolver

A recursive DNS resolver that steers clients toward lower-carbon service
endpoints. For each domain it aggregates RRsets from multiple upstream public
resolvers, geolocates every endpoint, attaches its grid's Marginal Operational
Emission Rate (MOER), and answers with the greenest address. This is the
*Multi-Resolver Greenest* strategy from *Leveraging DNS for Carbon-Aware Server
Selection* (Shakeri et al., University of Twente).

Built on BIND 9 + PostgreSQL (DLZ), inspired by
[ActiveDNS](https://github.com/MhrshadSh/ActiveDNS).

- Architecture and design decisions: [docs/architecture.md](docs/architecture.md)
- Build plan and status: [docs/ROADMAP.md](docs/ROADMAP.md)

> Status: early development (Phase 0).
