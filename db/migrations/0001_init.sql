-- CA-DNS initial schema: domains and their RRsets, endpoint geolocation,
-- grid-region carbon signals, the measurement queue, and database roles.
-- See docs/architecture.md §4.

-- Roles ----------------------------------------------------------------------
-- Created NOLOGIN; the migration runner sets LOGIN + password from the
-- environment. Roles are cluster-wide, hence the existence checks.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cadns_dlz') THEN
        CREATE ROLE cadns_dlz NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cadns_app') THEN
        CREATE ROLE cadns_app NOLOGIN;
    END IF;
END
$$;

COMMENT ON ROLE cadns_dlz IS 'BIND DLZ module: may only execute cadns.dlz_findzone / cadns.dlz_lookup';
COMMENT ON ROLE cadns_app IS 'CA-DNS Python services (collector, monitor, worker)';

DO $$
BEGIN
    EXECUTE format('REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO cadns_dlz, cadns_app', current_database());
END
$$;

CREATE SCHEMA cadns;
REVOKE ALL ON SCHEMA cadns FROM PUBLIC;
GRANT USAGE ON SCHEMA cadns TO cadns_dlz, cadns_app;

-- Helpers --------------------------------------------------------------------
-- Canonical form of a DNS name as stored: lowercase, no trailing dot.
CREATE FUNCTION cadns.normalize_name(name text) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    RETURN lower(rtrim(name, '.'));

-- Answer policy settings (single row) -----------------------------------------
CREATE TABLE cadns.settings (
    singleton      boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    -- Addresses returned per RRset type (the paper's Greenest strategy uses 1).
    answer_k       integer NOT NULL DEFAULT 1 CHECK (answer_k >= 1),
    -- Upper bound on answer TTLs: the carbon signal refresh interval.
    max_answer_ttl integer NOT NULL DEFAULT 300 CHECK (max_answer_ttl >= 1),
    -- Names used in the synthesised SOA/NS records (absolute, trailing dot).
    ns_name        text NOT NULL DEFAULT 'ns.cadns.invalid.' CHECK (ns_name LIKE '%.'),
    hostmaster     text NOT NULL DEFAULT 'hostmaster.cadns.invalid.' CHECK (hostmaster LIKE '%.'),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
INSERT INTO cadns.settings DEFAULT VALUES;

-- Domains ----------------------------------------------------------------------
CREATE TABLE cadns.domains (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            text COLLATE "C" NOT NULL UNIQUE
                    CHECK (name <> '' AND name = cadns.normalize_name(name)),
    -- pending: not measured yet; resolved: has addresses;
    -- nxdomain / nodata: negative result; failed: upstream errors.
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'resolved', 'nxdomain', 'nodata', 'failed')),
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    measured_at     timestamptz,
    last_queried_at timestamptz,
    hit_count       bigint NOT NULL DEFAULT 0 CHECK (hit_count >= 0)
);
CREATE INDEX domains_last_queried_at_idx ON cadns.domains (last_queried_at);

-- Grid regions and carbon signals ---------------------------------------------------
CREATE TABLE cadns.grid_regions (
    code       text PRIMARY KEY,
    name       text,
    provider   text NOT NULL DEFAULT 'watttime'
);

-- MOER time series per region; the answer policy uses the latest point.
CREATE TABLE cadns.carbon_signals (
    region_code    text NOT NULL REFERENCES cadns.grid_regions (code) ON UPDATE CASCADE ON DELETE CASCADE,
    point_time     timestamptz NOT NULL,
    moer_g_per_kwh double precision NOT NULL CHECK (moer_g_per_kwh >= 0),
    fetched_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (region_code, point_time)
);

-- Endpoints (one row per IP address) --------------------------------------------
CREATE TABLE cadns.endpoints (
    address        inet PRIMARY KEY
                   CHECK (masklen(address) = CASE family(address) WHEN 4 THEN 32 ELSE 128 END),
    lat            double precision CHECK (lat BETWEEN -90 AND 90),
    lon            double precision CHECK (lon BETWEEN -180 AND 180),
    city           text,
    country        char(2),
    asn            bigint CHECK (asn BETWEEN 0 AND 4294967295),
    -- Anycast addresses have no meaningful location; ranked as unknown MOER.
    is_anycast     boolean NOT NULL DEFAULT false,
    region_code    text REFERENCES cadns.grid_regions (code) ON UPDATE CASCADE ON DELETE SET NULL,
    geo_source     text CHECK (geo_source IN ('ipinfo_mmdb', 'ipinfo_api', 'manual')),
    geo_updated_at timestamptz,
    CHECK ((lat IS NULL) = (lon IS NULL))
);
CREATE INDEX endpoints_region_code_idx ON cadns.endpoints (region_code);

-- RRset records: one row per (domain, type, address, upstream resolver) ----------
CREATE TABLE cadns.rrset_records (
    domain_id   bigint NOT NULL REFERENCES cadns.domains (id) ON DELETE CASCADE,
    rtype       text NOT NULL CHECK (rtype IN ('A', 'AAAA')),
    address     inet NOT NULL REFERENCES cadns.endpoints (address),
    resolver    inet NOT NULL,
    -- TTL as received (minimum over the CNAME chain).
    ttl         integer NOT NULL CHECK (ttl >= 0),
    resolved_at timestamptz NOT NULL,
    expires_at  timestamptz NOT NULL CHECK (expires_at >= resolved_at),
    PRIMARY KEY (domain_id, rtype, address, resolver),
    CHECK ((rtype = 'A' AND family(address) = 4) OR (rtype = 'AAAA' AND family(address) = 6))
);
CREATE INDEX rrset_records_address_idx ON cadns.rrset_records (address);
CREATE INDEX rrset_records_expires_at_idx ON cadns.rrset_records (expires_at);

-- Measurement queue (architecture ADR-3; consumed from Phase 4) --------------------
CREATE TABLE cadns.measurement_queue (
    domain          text COLLATE "C" PRIMARY KEY CHECK (domain = cadns.normalize_name(domain)),
    reason          text NOT NULL CHECK (reason IN ('miss', 'expired')),
    state           text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'dead')),
    enqueued_at     timestamptz NOT NULL DEFAULT now(),
    attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    locked_by       text,
    locked_at       timestamptz,
    last_error      text,
    CHECK ((locked_by IS NULL) = (locked_at IS NULL))
);
CREATE INDEX measurement_queue_ready_idx ON cadns.measurement_queue (next_attempt_at, enqueued_at)
    WHERE state = 'pending';

-- Privileges -------------------------------------------------------------------
-- cadns_dlz gets nothing on tables: it reads only through the SECURITY DEFINER
-- DLZ functions (0002). cadns_app gets DML on everything in the schema.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA cadns TO cadns_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA cadns TO cadns_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA cadns GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO cadns_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA cadns GRANT USAGE, SELECT ON SEQUENCES TO cadns_app;
