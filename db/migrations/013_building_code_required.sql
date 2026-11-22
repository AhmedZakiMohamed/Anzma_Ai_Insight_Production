-- G4: require stable, unique building codes after operators register legacy rows.
-- This migration is additive and refuses to guess codes for existing buildings.
BEGIN;

DO $$
DECLARE missing_ids TEXT;
BEGIN
    SELECT string_agg(building_id::text, ', ' ORDER BY building_id)
      INTO missing_ids
      FROM buildings
     WHERE building_code IS NULL OR btrim(building_code) = '';
    IF missing_ids IS NOT NULL THEN
        RAISE EXCEPTION 'Migration 013 requires building_code for every building. Missing building_id(s): %. Register stable codes with scripts/ingestion_admin.py register-building --building-id <id> --building-code <code>, then retry.', missing_ids
            USING ERRCODE = '23502';
    END IF;
END $$;

-- The partial unique index from migration 006 is now effectively a full unique
-- index because NULL values are disallowed.
ALTER TABLE buildings ALTER COLUMN building_code SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_buildings_building_code_all ON buildings(building_code);

INSERT INTO schema_migrations(version) VALUES ('013_building_code_required')
ON CONFLICT (version) DO NOTHING;

COMMIT;
