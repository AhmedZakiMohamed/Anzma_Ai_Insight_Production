# Operational incidents

## Migration runner target incident — 2026-11-23

### What happened

During the G4 disposable-database rehearsal, `scripts/apply_migrations.py` was invoked from the host after loading `.env`. The environment resolved to `PGHOST=localhost`, `PGPORT=5432`, and `PGDATABASE=anzma`. Docker published the shared `anzma-insight-db-1` container on host port 5432, so the runner connected to that database rather than to the disposable G4 database.

### Impact verified by the operator

The runner skipped migrations 006–011, then applied migrations 012 and 013 and registered the two seeded building codes `MAIN-TOWER` and `DATA-CENTER-ANNEX`. A subsequent operator read-only check found migrations 006–013 recorded, with telemetry 4669, events 955, and assets 12. The operator chose to keep migrations 012 and 013 and the building codes. No rollback was requested or performed.

### Prevention

- Run database commands inside the intended disposable Compose project's container, using its project name explicitly.
- Before a database command, identify the Compose project, container name, published ports, and database name. Verify that the container belongs to the disposable project.
- Do not set or pass `PGHOST`/`PGPORT` to localhost for database commands. Do not use host ports 5432, 8000, or 8080; allocate and verify free ports in 18000–19999 for disposable services that need host access.
- The migration runner now rejects local host targets unless explicitly allowed for the matching database, prints the resolved target and migration-ledger count before applying changes, requires confirmation for non-container execution, and supports a read-only `--dry-run` listing.

