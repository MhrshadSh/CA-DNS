-- Minimum lifetime of stored records (Phase 5; docs/architecture.md ADR-10).
--
-- The worker stores expires_at = resolved_at + greatest(ttl, min_record_ttl).
-- rrset_records.ttl keeps the TTL as received. 0 respects upstream TTLs
-- exactly; CDN hostnames often use 20-60 s, which makes the monitor re-measure
-- active names that often. Changes apply to later measurements.
ALTER TABLE cadns.settings
    ADD COLUMN min_record_ttl integer NOT NULL DEFAULT 0
        CHECK (min_record_ttl BETWEEN 0 AND 86400);

COMMENT ON COLUMN cadns.settings.min_record_ttl IS
    'Records stay servable at least this long after a measurement (seconds; 0 = upstream TTL)';
