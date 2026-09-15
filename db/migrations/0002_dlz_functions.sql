-- Answer policy used by the BIND DLZ module (docs/architecture.md ADR-1, ADR-5).
--
-- BIND calls dlz_findzone(name) while probing a query name from the longest
-- suffix down, then dlz_lookup(zone, name) for the zone it found. Both are
-- SECURITY DEFINER so cadns_dlz needs no table privileges, and use SQL-standard
-- bodies, which are parsed at creation time (no search_path surprises).

-- True if CA-DNS should answer authoritatively for this name: at least one
-- A/AAAA RRset is stored and every stored RRset type still has a fresh record.
-- If one type has expired (say AAAA), serving the zone would turn AAAA queries
-- into authoritative NODATA, so the name falls back to recursion until
-- re-measured.
CREATE FUNCTION cadns.dlz_findzone(zone_name text) RETURNS boolean
    LANGUAGE sql STABLE STRICT SECURITY DEFINER
    SET search_path = cadns, pg_temp
BEGIN ATOMIC
    SELECT coalesce(bool_and(per_type.fresh), false)
    FROM (
        SELECT max(r.expires_at) > now() AS fresh
        FROM cadns.domains d
        JOIN cadns.rrset_records r ON r.domain_id = d.id
        WHERE d.name = cadns.normalize_name(zone_name)
        GROUP BY r.rtype
    ) AS per_type;
END;

COMMENT ON FUNCTION cadns.dlz_findzone(text) IS
    'DLZ findzone: true if fresh A/AAAA data exists for every stored RRset type of the name';

-- Records for a name inside a zone found by dlz_findzone. Only the apex ('@')
-- has data; child names return nothing (NXDOMAIN).
--
-- Apex answer:
--   SOA, NS  synthesised from cadns.settings
--   A, AAAA  per type, the answer_k fresh addresses with the lowest MOER
--            (latest carbon signal of the endpoint's region). Unknown MOER
--            (no endpoint row, no region, no signal, anycast) ranks last;
--            ties are broken randomly.
-- TTL: per RRset, min(remaining TTL of the returned records, max_answer_ttl),
-- at least 1 s. SOA/NS use the smallest RRset TTL, which is also the SOA
-- minimum, so negative answers never outlive the data.
CREATE FUNCTION cadns.dlz_lookup(zone_name text, record_name text)
    RETURNS TABLE (ttl integer, type text, data text)
    LANGUAGE sql VOLATILE STRICT SECURITY DEFINER
    SET search_path = cadns, pg_temp
BEGIN ATOMIC
    WITH cfg AS (
        SELECT s.answer_k, s.max_answer_ttl, s.ns_name, s.hostmaster
        FROM cadns.settings s
    ),
    dom AS (
        SELECT d.id
        FROM cadns.domains d
        WHERE d.name = cadns.normalize_name(zone_name)
          AND (record_name IN ('@', '') OR cadns.normalize_name(record_name) = d.name)
          AND cadns.dlz_findzone(zone_name)
    ),
    -- Union over upstream resolvers: one row per fresh address.
    candidates AS (
        SELECT r.rtype, r.address, max(r.expires_at) AS expires_at, max(r.resolved_at) AS resolved_at
        FROM cadns.rrset_records r
        JOIN dom ON r.domain_id = dom.id
        WHERE r.expires_at > now()
        GROUP BY r.rtype, r.address
    ),
    ranked AS (
        SELECT c.rtype, c.address, c.expires_at, c.resolved_at,
               row_number() OVER (
                   PARTITION BY c.rtype
                   ORDER BY latest.moer_g_per_kwh ASC NULLS LAST, random()
               ) AS rank
        FROM candidates c
        LEFT JOIN cadns.endpoints e ON e.address = c.address AND NOT e.is_anycast
        LEFT JOIN LATERAL (
            SELECT cs.moer_g_per_kwh
            FROM cadns.carbon_signals cs
            WHERE cs.region_code = e.region_code
            ORDER BY cs.point_time DESC
            LIMIT 1
        ) AS latest ON true
    ),
    chosen AS (
        SELECT ranked.*
        FROM ranked, cfg
        WHERE ranked.rank <= cfg.answer_k
    ),
    rrsets AS (
        SELECT c.rtype, c.address, c.rank,
               greatest(1, least(
                   cfg.max_answer_ttl,
                   floor(extract(epoch FROM min(c.expires_at) OVER (PARTITION BY c.rtype) - now()))
               ))::integer AS ttl
        FROM chosen c, cfg
    ),
    apex AS (
        SELECT min(r.ttl) AS ttl,
               (SELECT floor(extract(epoch FROM max(c.resolved_at)))::bigint % 4294967296 FROM chosen c) AS serial
        FROM rrsets r
    )
    SELECT answer.ttl, answer.type, answer.data
    FROM (
        SELECT a.ttl, 'SOA' AS type,
               format('%s %s %s 3600 600 86400 %s', cfg.ns_name, cfg.hostmaster, a.serial, a.ttl) AS data,
               0 AS section, 0::bigint AS rank
        FROM apex a, cfg
        WHERE a.ttl IS NOT NULL
        UNION ALL
        SELECT a.ttl, 'NS', cfg.ns_name, 1, 0
        FROM apex a, cfg
        WHERE a.ttl IS NOT NULL
        UNION ALL
        SELECT r.ttl, r.rtype, host(r.address), CASE r.rtype WHEN 'A' THEN 2 ELSE 3 END, r.rank
        FROM rrsets r
    ) AS answer
    ORDER BY answer.section, answer.rank;
END;

COMMENT ON FUNCTION cadns.dlz_lookup(text, text) IS
    'DLZ lookup: synthesised SOA/NS plus the greenest answer_k A/AAAA records at the zone apex';

REVOKE ALL ON FUNCTION cadns.dlz_findzone(text), cadns.dlz_lookup(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cadns.dlz_findzone(text), cadns.dlz_lookup(text, text) TO cadns_dlz, cadns_app;
