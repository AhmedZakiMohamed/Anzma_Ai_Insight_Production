-- ADR-0001 follow-ups 1 and 2: door/access vocabulary and a data-driven open/close pairing.
--   * adds the catalogue types door_opened and access_granted (ACCESS_CONTROL only);
--   * adds event_types.event_class, event_types.outage_class, event_types.resolves_event_type_id;
--   * backfills the existing 12 catalogue types, so the DB states what the three hand-maintained
--     code lists (dashboard_core_pg.FAULT_RECOVERY_PAIRS, chat_facts.PAIRS, integrity_rules.CLOSERS,
--     plus the v_camera_status view) already encode.
-- Additive and idempotent. The legacy code lists are NOT changed here; tests/unit/test_event_catalog_parity.py
-- and tests/integration/test_event_catalogue_migration.py fail if the DB and those lists ever diverge.
-- No event_records, telemetry or alert rows are read or modified. Forward-only like 003-013.
--
-- Vocabulary:
--   event_class   ACTIVITY | FAULT_OPEN | FAULT_CLOSE | HEARTBEAT | STATE_REPORT   (CANONICAL_DATA_MODEL.md section 10)
--   outage_class  AVAILABILITY | FAULT   (only for FAULT_OPEN / FAULT_CLOSE types; ADR-0001 D-05 and open question h)
--                 AVAILABILITY = explicit offline/online transition (camera_offline/online, link_down/up):
--                                eligible for devices.offline_frequency.
--                 FAULT        = fault/alarm and its recovery (hvac_fault/recovered, fire_alarm_triggered/alarm_cleared):
--                                never evidence that equipment is Offline.
-- Event types that are not in this catalogue (operator-added) keep event_class NULL = "unclassified": no pairing,
-- never eligible for offline analytics. The migration does not guess a class for them.
BEGIN;

-- 1. Vocabulary (names only; ids come from the identity sequence)
INSERT INTO event_types (name) VALUES ('door_opened'), ('access_granted')
ON CONFLICT (name) DO NOTHING;

INSERT INTO event_type_system_compat (event_type_id, system_type_id)
SELECT et.event_type_id, st.system_type_id
  FROM event_types et
  JOIN system_types st ON st.code = 'ACCESS_CONTROL'
 WHERE et.name IN ('door_opened', 'access_granted')
ON CONFLICT DO NOTHING;

-- 2. Columns
ALTER TABLE event_types
    ADD COLUMN IF NOT EXISTS event_class TEXT,
    ADD COLUMN IF NOT EXISTS outage_class TEXT,
    ADD COLUMN IF NOT EXISTS resolves_event_type_id BIGINT
        REFERENCES event_types(event_type_id) ON DELETE RESTRICT;

-- 3. Backfill. Only rows still unclassified are touched; operator-provided values are never overwritten.
UPDATE event_types et
   SET event_class = k.event_class,
       outage_class = k.outage_class
  FROM (VALUES
        ('door_forced',          'ACTIVITY',   NULL),
        ('door_held_open',       'ACTIVITY',   NULL),
        ('access_denied',        'ACTIVITY',   NULL),
        ('door_opened',          'ACTIVITY',   NULL),
        ('access_granted',       'ACTIVITY',   NULL),
        ('heartbeat',            'HEARTBEAT',  NULL),
        ('link_down',            'FAULT_OPEN',  'AVAILABILITY'),
        ('link_up',              'FAULT_CLOSE', 'AVAILABILITY'),
        ('camera_offline',       'FAULT_OPEN',  'AVAILABILITY'),
        ('camera_online',        'FAULT_CLOSE', 'AVAILABILITY'),
        ('fire_alarm_triggered', 'FAULT_OPEN',  'FAULT'),
        ('alarm_cleared',        'FAULT_CLOSE', 'FAULT'),
        ('hvac_fault',           'FAULT_OPEN',  'FAULT'),
        ('hvac_recovered',       'FAULT_CLOSE', 'FAULT')
       ) AS k(name, event_class, outage_class)
 WHERE et.name = k.name
   AND et.event_class IS NULL;

UPDATE event_types closer
   SET resolves_event_type_id = opener.event_type_id
  FROM (VALUES
        ('link_up',         'link_down'),
        ('camera_online',   'camera_offline'),
        ('alarm_cleared',   'fire_alarm_triggered'),
        ('hvac_recovered',  'hvac_fault')
       ) AS p(closer_name, opener_name)
  JOIN event_types opener ON opener.name = p.opener_name
 WHERE closer.name = p.closer_name
   AND closer.resolves_event_type_id IS NULL;

-- 4. Constraints (guarded so the file can be re-run safely)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'event_types_event_class_check'
                      AND conrelid = 'public.event_types'::regclass) THEN
        ALTER TABLE event_types ADD CONSTRAINT event_types_event_class_check
            CHECK (event_class IS NULL
                   OR event_class IN ('ACTIVITY','FAULT_OPEN','FAULT_CLOSE','HEARTBEAT','STATE_REPORT'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'event_types_outage_class_check'
                      AND conrelid = 'public.event_types'::regclass) THEN
        ALTER TABLE event_types ADD CONSTRAINT event_types_outage_class_check
            CHECK (outage_class IS NULL OR outage_class IN ('AVAILABILITY','FAULT'));
    END IF;
    -- outage_class exists exactly for the open/close classes
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'event_types_outage_class_shape_check'
                      AND conrelid = 'public.event_types'::regclass) THEN
        ALTER TABLE event_types ADD CONSTRAINT event_types_outage_class_shape_check
            CHECK ((COALESCE(event_class, '') IN ('FAULT_OPEN','FAULT_CLOSE')) = (outage_class IS NOT NULL));
    END IF;
    -- a type resolves another type exactly when it is a FAULT_CLOSE
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'event_types_pair_shape_check'
                      AND conrelid = 'public.event_types'::regclass) THEN
        ALTER TABLE event_types ADD CONSTRAINT event_types_pair_shape_check
            CHECK ((COALESCE(event_class, '') = 'FAULT_CLOSE') = (resolves_event_type_id IS NOT NULL));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'event_types_resolves_not_self_check'
                      AND conrelid = 'public.event_types'::regclass) THEN
        ALTER TABLE event_types ADD CONSTRAINT event_types_resolves_not_self_check
            CHECK (resolves_event_type_id IS NULL OR resolves_event_type_id <> event_type_id);
    END IF;
END $$;

-- one closer per opener
CREATE UNIQUE INDEX IF NOT EXISTS uq_event_types_resolves
    ON event_types(resolves_event_type_id)
 WHERE resolves_event_type_id IS NOT NULL;

-- 5. Cross-row invariant a CHECK cannot express: a closer must resolve a FAULT_OPEN of the same outage_class.
DO $$
DECLARE bad TEXT;
BEGIN
    SELECT string_agg(c.name, ', ' ORDER BY c.name) INTO bad
      FROM event_types c
      JOIN event_types o ON o.event_type_id = c.resolves_event_type_id
     WHERE o.event_class IS DISTINCT FROM 'FAULT_OPEN'
        OR o.outage_class IS DISTINCT FROM c.outage_class;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'Migration 014: closer type(s) % do not resolve a FAULT_OPEN type of the same outage_class.', bad
            USING ERRCODE = '23514';
    END IF;
END $$;

INSERT INTO schema_migrations(version) VALUES ('014_event_catalogue_pairing')
ON CONFLICT (version) DO NOTHING;

COMMIT;

-- Manual rollback (never run automatically; destroys the pairing data, event_records are untouched):
--   BEGIN;
--   DROP INDEX IF EXISTS uq_event_types_resolves;
--   ALTER TABLE event_types DROP COLUMN resolves_event_type_id, DROP COLUMN outage_class, DROP COLUMN event_class;
--   DELETE FROM event_type_system_compat WHERE event_type_id IN
--       (SELECT event_type_id FROM event_types WHERE name IN ('door_opened','access_granted'));
--   DELETE FROM event_types WHERE name IN ('door_opened','access_granted');   -- fails if any event_records use them
--   DELETE FROM schema_migrations WHERE version = '014_event_catalogue_pairing';
--   COMMIT;
