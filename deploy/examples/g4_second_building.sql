-- Disposable G4 proof data: create a distinct building/site after migration 013.
-- Run only against an explicitly selected test/staging database.
BEGIN;

INSERT INTO buildings (name, building_code, timezone, work_start_local, work_end_local, non_working_weekdays)
VALUES ('G4 North Campus', 'G4-NORTH-CAMPUS', 'America/New_York', TIME '07:00', TIME '16:00', ARRAY[6,7]::smallint[])
ON CONFLICT (building_code) DO NOTHING;

INSERT INTO locations (building_id, floor, name)
SELECT building_id, 1, 'G4 Mechanical Room' FROM buildings WHERE building_code='G4-NORTH-CAMPUS'
ON CONFLICT (building_id, name) DO NOTHING;

INSERT INTO assets (asset_code, name, asset_type, system_type_id, location_id)
SELECT v.asset_code, v.name, 'ahu', st.system_type_id, l.location_id
  FROM (VALUES ('G4-AHU-01', 'G4 Air Handler 1'), ('G4-AHU-02', 'G4 Air Handler 2')) AS v(asset_code,name)
  JOIN system_types st ON st.code='HVAC'
  JOIN locations l ON l.name='G4 Mechanical Room'
  JOIN buildings b ON b.building_id=l.building_id AND b.building_code='G4-NORTH-CAMPUS'
ON CONFLICT (asset_code) DO NOTHING;

COMMIT;
