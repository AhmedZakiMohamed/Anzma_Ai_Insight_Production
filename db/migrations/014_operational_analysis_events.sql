-- G1 canonical event vocabulary for physical door openings and explicit outages.
-- Offline transitions are distinct from source silence and must come from upstream evidence.
BEGIN;

INSERT INTO event_types (name) VALUES ('door_opened'), ('access_granted'), ('offline_start'), ('offline_end')
ON CONFLICT (name) DO NOTHING;

-- A physical opening belongs to access-control assets only. Explicit device outage
-- transitions are valid across the currently registered system catalogue.
INSERT INTO event_type_system_compat (event_type_id, system_type_id)
SELECT et.event_type_id, st.system_type_id
  FROM event_types et
 CROSS JOIN system_types st
 WHERE et.name IN ('offline_start', 'offline_end')
ON CONFLICT DO NOTHING;

INSERT INTO event_type_system_compat (event_type_id, system_type_id)
SELECT et.event_type_id, st.system_type_id
  FROM event_types et
  JOIN system_types st ON st.code = 'ACCESS_CONTROL'
 WHERE et.name IN ('door_opened', 'access_granted')
ON CONFLICT DO NOTHING;

INSERT INTO schema_migrations(version) VALUES ('014_operational_analysis_events')
ON CONFLICT (version) DO NOTHING;

COMMIT;
