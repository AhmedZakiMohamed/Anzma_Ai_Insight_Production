# Changelog

All notable changes to Anzma AI Insight Core are recorded here. This file is maintained for the proposed v0.1 release; no tag has been created.

## [0.1.0] — proposed, untagged

### Added

- Versioned Core ingestion at `POST /api/v1/ingest/batches` with per-record results, exact batch replay recognition, rejection audit, immutable telemetry conflicts, source-scoped external asset mapping, per-source hashed API keys, and the documented v1 contract.
- Additive migrations 006–008 and an advisory-locked migration runner for existing databases.
- Separate named automated tests for each of the 16 ingestion contract conformance vectors.
- Clean-clone Compose deployment and first-batch instructions, configurable host ports, and `/health` / `/ready` verification steps.
- Functional, migration, deployment, data-validation, and known-issues evidence for Phase 0.
- Environment-configured bearer authentication for normal REST and Socket.IO access, with an eight-hour HttpOnly browser session cookie; ingestion source-key authentication remains separate.
- Loopback-only default host bindings for PostgreSQL and Ollama.
- API snapshot comparison with a documented `1e-12` numeric tolerance and exact comparison for nonnumeric fields.
- Authenticated, read-only E5 endpoints for series freshness, source liveness, and gap history, plus one dashboard evidence panel; existing KPI, alert, equipment-health, and chat contracts remain unchanged.
- Migration 014 (`014_event_catalogue_pairing.sql`): catalogue types `door_opened` and `access_granted` (ACCESS_CONTROL), `event_types.event_class`, `outage_class` and `resolves_event_type_id`, backfilled for the 12 seed types. The legacy pair lists are unchanged; parity tests compare them with the database (ADR-0001 follow-ups 1 and 2).
- `devices.offline_frequency` analytics tool (`app/domain/tools/devices_offline_frequency.py`): outage sessions per device (repeated alarms merged, open-ended outages as lower bounds, per-session and total downtime, evidence refs), silence is never an outage. Shared `periods.build_period`; see `docs/DEVICES_OFFLINE_FREQUENCY.md`.

### Fixed

- `scripts/apply_migrations.py --help` now displays usage without requiring a database connection.
- Dashboard smoke query now uses the latest supported 31-day interval, even when test data contains a much older legacy timestamp.
- The CRAC recovered-history test now uses an isolated fixture and separately checks active-alert status, removing its test-order assumption.
- Compose host ports can be changed with `DB_HOST_PORT`, `API_HOST_PORT`, and `OLLAMA_HOST_PORT` to avoid local port collisions.
- Deployment docs now describe migrations 006–008 for existing volumes, service/readiness checks, and source/key/mapping provisioning.
- Release hardening adds explicit normal API authentication and limits PostgreSQL/Ollama host publication to loopback unless an operator overrides the bind address.

### Compatibility

- Legacy `POST /events` and `POST /alerts` remain available and passed the integration suite.
- Existing H0 telemetry/event/alert/incident and catalogue rows remained unchanged in the disposable migration rehearsal. Legacy quality/provenance fields remain NULL where their historical values are unknown.

### Verification limits

- No shared/live database migration, push, or tag was performed.
- General REST/Socket.IO authentication, production deployment security, successful live Ollama inference, and a real device connector remain outstanding; details are in [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) and [`docs/RELEASE_CHECKLIST_v0.1.md`](docs/RELEASE_CHECKLIST_v0.1.md).
