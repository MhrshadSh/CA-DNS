-- Demo data for *.example.test. Idempotent: re-running refreshes timestamps.
--
-- Addresses are documentation prefixes (RFC 5737, RFC 3849). Region codes
-- follow WattTime naming; MOER values are illustrative, not real measurements.
-- Records get a 1-day TTL so the demo stays fresh (answers are still capped at
-- settings.max_answer_ttl).
--
--   www.example.test    A from four regions + one anycast address, AAAA from two:
--                       the greenest is 192.0.2.10 / 2001:db8::10 (FR)
--   cdn.example.test    two endpoints in the same region (random tie) + one
--                       address without geolocation (ranked last)
--   stale.example.test  expired records: dlz_findzone is false, BIND recurses

DELETE FROM cadns.domains WHERE name LIKE '%.example.test';

INSERT INTO cadns.grid_regions (code, name) VALUES
    ('SE',          'Sweden'),
    ('FR',          'France'),
    ('DE',          'Germany'),
    ('PJM_DC',      'PJM Washington DC')
ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name;

INSERT INTO cadns.carbon_signals (region_code, point_time, moer_g_per_kwh)
SELECT region, date_trunc('minute', now()), moer
FROM (VALUES ('SE', 35.0), ('FR', 60.0), ('DE', 610.0), ('PJM_DC', 790.0)) AS v (region, moer)
ON CONFLICT (region_code, point_time) DO UPDATE SET moer_g_per_kwh = EXCLUDED.moer_g_per_kwh;

INSERT INTO cadns.endpoints (address, lat, lon, city, country, is_anycast, region_code, geo_source, geo_updated_at)
VALUES
    ('192.0.2.10',    48.86,    2.35, 'Paris',      'FR', false, 'FR',     'manual', now()),
    ('198.51.100.20', 50.11,    8.68, 'Frankfurt',  'DE', false, 'DE',     'manual', now()),
    ('203.0.113.30',  38.90,  -77.04, 'Washington', 'US', false, 'PJM_DC', 'manual', now()),
    ('192.0.2.40',    NULL,     NULL, NULL,         NULL, true,  NULL,     'manual', now()),
    ('2001:db8::10',  48.86,    2.35, 'Paris',      'FR', false, 'FR',     'manual', now()),
    ('2001:db8::30',  38.90,  -77.04, 'Washington', 'US', false, 'PJM_DC', 'manual', now()),
    ('192.0.2.50',    59.33,   18.07, 'Stockholm',  'SE', false, 'SE',     'manual', now()),
    ('192.0.2.51',    59.33,   18.07, 'Stockholm',  'SE', false, 'SE',     'manual', now()),
    ('198.51.100.99', NULL,     NULL, NULL,         NULL, false, NULL,     NULL,     NULL),
    ('203.0.113.77',  50.11,    8.68, 'Frankfurt',  'DE', false, 'DE',     'manual', now())
ON CONFLICT (address) DO UPDATE SET
    lat = EXCLUDED.lat, lon = EXCLUDED.lon, city = EXCLUDED.city, country = EXCLUDED.country,
    is_anycast = EXCLUDED.is_anycast, region_code = EXCLUDED.region_code,
    geo_source = EXCLUDED.geo_source, geo_updated_at = EXCLUDED.geo_updated_at;

INSERT INTO cadns.domains (name, status, measured_at) VALUES
    ('www.example.test',   'resolved', now()),
    ('cdn.example.test',   'resolved', now()),
    ('stale.example.test', 'resolved', now() - interval '2 hours');

INSERT INTO cadns.rrset_records (domain_id, rtype, address, resolver, ttl, resolved_at, expires_at)
SELECT d.id, r.rtype, r.address::inet, r.resolver::inet, r.ttl, r.resolved_at, r.resolved_at + make_interval(secs => r.ttl)
FROM (VALUES
    ('www.example.test',   'A',    '192.0.2.10',    '8.8.8.8',        86400, now()),
    ('www.example.test',   'A',    '192.0.2.10',    '1.1.1.1',        86400, now()),
    ('www.example.test',   'A',    '198.51.100.20', '9.9.9.9',        86400, now()),
    ('www.example.test',   'A',    '203.0.113.30',  '208.67.222.222', 86400, now()),
    ('www.example.test',   'A',    '192.0.2.40',    '45.90.28.243',   86400, now()),
    ('www.example.test',   'AAAA', '2001:db8::10',  '8.8.8.8',        86400, now()),
    ('www.example.test',   'AAAA', '2001:db8::30',  '9.9.9.9',        86400, now()),
    ('cdn.example.test',   'A',    '192.0.2.50',    '8.8.8.8',        86400, now()),
    ('cdn.example.test',   'A',    '192.0.2.51',    '1.1.1.1',        86400, now()),
    ('cdn.example.test',   'A',    '198.51.100.99', '9.9.9.9',        86400, now()),
    ('stale.example.test', 'A',    '203.0.113.77',  '8.8.8.8',        300,   now() - interval '2 hours')
) AS r (domain, rtype, address, resolver, ttl, resolved_at)
JOIN cadns.domains d ON d.name = r.domain;
