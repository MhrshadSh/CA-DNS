-- Measurement worker support (Phase 3; docs/architecture.md ADR-9).

-- Negative (nxdomain/nodata) or failed measurements: don't re-measure before
-- this time. The collector (Phase 4) checks it before enqueueing a miss.
ALTER TABLE cadns.domains ADD COLUMN retry_after timestamptz;
COMMENT ON COLUMN cadns.domains.retry_after IS
    'Do not re-measure before this time (set for nxdomain, nodata and failed results)';

-- Grid region per location (WattTime region-from-loc), cached permanently.
-- IP geolocation returns city-level coordinates, so many endpoints share a
-- rounded location. region_code NULL: the location is outside coverage.
CREATE TABLE cadns.location_regions (
    provider     text NOT NULL DEFAULT 'watttime',
    lat_e2       integer NOT NULL CHECK (lat_e2 BETWEEN -9000 AND 9000),   -- latitude * 100
    lon_e2       integer NOT NULL CHECK (lon_e2 BETWEEN -18000 AND 18000), -- longitude * 100
    region_code  text REFERENCES cadns.grid_regions (code) ON UPDATE CASCADE ON DELETE CASCADE,
    looked_up_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, lat_e2, lon_e2)
);
