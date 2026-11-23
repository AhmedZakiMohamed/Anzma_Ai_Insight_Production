# Change coordination

This note defines ownership and records cross-owner changes. Preserve each owner's existing behavior; make small additive patches and record review-sensitive touches here.

## Database command safety rules

- Run every database command inside the intended disposable Compose project's container with `docker compose -p <project> ... exec`.
- Never export or pass `PGHOST` or `PGPORT` pointing at localhost.
- Before a database command, state the target container name, `docker port` output, database name, and confirm the container belongs to the disposable project.
- Never use host ports 5432, 8000, or 8080 for disposable services. Select ports in 18000–19999 and verify they are free first.
- Do not run database commands against shared/live projects during local verification. If target identity is ambiguous, stop and resolve it before connecting.

## Ownership

| Area | Owner | Coordination rule |
|---|---|---|
| Model interface and Ollama adapter | Amr | Preserve `ModelProvider`, `OllamaProvider`, existing env variables and tests. |
| AI explanation, recommendations, evidence and validation | Amr | Any new evidence input must be registered in grounding/validation and tested as supported; unsupported claims must remain rejected. |
| Chat and security | Amr | Auth behavior/interface changes require a design note and human approval first. |
| Compose defaults, deployment env and `docs/DAY3_*` reports | Amr | Do not rewrite DAY3 evidence. Record contradictions elsewhere. |
| Migrations and operational data model | Ahmed / project owner | Add the next numbered migration only; additive and idempotent. Never edit a committed migration. |
| Dashboard, freshness, integration tests and operational docs | Ahmed / project owner | Preserve existing response shapes and parity; log edits to Amr-owned tests/files for review. |

## Interfaces and protected configuration

- `app/domain/ai/model_provider.py` is the replaceable model boundary; `OllamaProvider` is the current adapter. Changes to the provider contract require a design note and human approval.
- Grounding and validation own supported evidence provenance. New fields supplied to AI/explanation inputs must be registered and have deterministic positive and negative grounding tests.
- Ingestion uses source-scoped credentials and its v1 contract. Operator REST/Socket.IO authentication uses the deployment bearer token/session flow; do not merge these credentials.
- Public API response shapes, authentication behavior, and Compose defaults are approval-gated. Dashboard implementation details may change only while preserving their asserted behavior and response contracts.

The following environment names are compatibility interfaces and must not be renamed or removed: `OLLAMA_HOST`, `OLLAMA_MODEL`, `API_PGDATABASE`, `API_BIND_ADDRESS`, `ANZMA_API_TOKEN`, `ANZMA_AUTH_DISABLED`. Keep the existing Compose healthchecks. Other documented `ANZMA_*` settings must likewise retain their stated semantics unless approved.

## Applying migrations to an existing database

Fresh-volume Compose initialization applies mounted SQL files once; it does not migrate an existing volume automatically. For a pre-existing database, first identify the exact Compose project/file and API service, take and verify a backup, inspect the migration runner's baseline behavior, then run the official runner against that project:

```sh
docker compose --env-file .env -p "$PROJECT" -f "$COMPOSE_FILE" exec -T api python scripts/apply_migrations.py
```

Set `PROJECT` and `COMPOSE_FILE` to the intended deployment. The API container receives its database connection settings from that Compose project. Never run this command against a shared/live database as part of disposable verification. Confirm the runner output and schema ledger afterward; if the pre-ledger baseline is ambiguous, stop rather than inventing ledger history.

For migration 013 specifically, first run `SELECT building_id FROM buildings WHERE building_code IS NULL ORDER BY building_id;` and obtain stable codes from the site operator. Register every returned row before the runner:

```sh
docker compose --env-file .env -p "$PROJECT" -f "$COMPOSE_FILE" exec -T api \
  python scripts/ingestion_admin.py register-building --building-id <id> --building-code <stable-code>
docker compose --env-file .env -p "$PROJECT" -f "$COMPOSE_FILE" exec -T api python scripts/apply_migrations.py
```

## Change log

| File | What changed | Why | Commit |
|---|---|---|---|
| `scripts/backup_db.sh` | Made retention mtime lookup GNU-first, BSD-fallback, numeric-only; invalid values warn and skip. | Fix Linux retention failure without deleting files on invalid metadata. | `18ac4d0` |
| `tests/unit/test_backup_retention.py` | Added malformed-mtime regression coverage; existing assertions retained. | Prove malformed metadata is safely skipped. | `18ac4d0` |
| `tests/integration/test_frontend_api.py` | Amr's test updated to match dashboard limit 200, please review. | Match the deliberate `6f190e8` dashboard request while keeping the assertion's purpose unchanged. | `0476405` |
| `tests/integration/test_freshness_job.py`, `test_freshness_gaps.py`, `test_source_liveness.py` | Moved injected test time just after test start. | Keep fixtures later than migration-created default validity timestamps. | `76f0169` |
| `docs/BACKUP.md` | Documented the prior Linux retention defect. | Warn operators about earlier script versions. | `dfc8ddf` |
| `docs/CHANGE_COORDINATION.md`, `docs/PHASE1_STATUS.md` | Added ownership, interface, migration, Amr-overlap and baseline records. | Coordinate continuation from main and preserve evidence. | This documentation commit |
| `db/migrations/012_asset_state_shadow.sql`, `scripts/verify_schema.sql` | Added the additive shadow-state tables, constraints, indexes, immutable-history trigger, and schema checks. | Store G3 projection without rewriting historical facts or changing existing readers. | `f332332` |
| `app/domain/asset_state.py`, `app/services/asset_state.py`, `app/repositories/dashboard_core_pg.py`, `scripts/freshness_job.py` | Added bucket-preserving projection and a separately committed shadow stage after freshness; refactored the dashboard query to expose per-asset rows while preserving its counts. | Keep current dashboard logic authoritative and isolate state-stage failure. | `ba35301` |
| `deploy/compose/docker-compose.yml` | Added only the migration 012 init mount. | Initialize the additive schema on new empty volumes; no Compose defaults changed. | `ba35301` |
| `tests/unit/test_asset_state.py`, `tests/integration/test_asset_state.py`, `tests/unit/test_freshness_job.py` | Added derivation, persistence, baseline/transition, immutability, replay/concurrency, evidence, and job-failure isolation checks. | Verify approved G3 shadow semantics on unit and disposable database paths. | `1737e06` |
| `app/services/pipeline.py` | Added a private evidence-context attachment before the existing explanation validation call. | Make cited provenance/freshness available internally without changing response fields. | `22ee886` |
| `app/domain/evidence/grounding.py` | Registered the private G3 data-path values and UTC timestamp forms with grounding. | Keep supported internal evidence usable while preserving rejection of unsupported claims. | `22ee886` |
| `app/domain/evidence/validation.py` | Added cited-UNCERTAIN sufficiency assessment; it changes sufficiency only when final citations refer to an UNCERTAIN telemetry row. | Disclose uncertainty without downgrading grounding or changing response shape. | `22ee886` |
| `scripts/state_parity_report.py` | Added a read-only seeded comparator and savepoint-rolled-back synthetic scenarios. | Show the derived operational dimension matches existing dashboard buckets. | `22ee886` |
| `tests/unit/test_asset_state_grounding.py`, `tests/integration/test_asset_state.py` | Added positive/negative grounding and citation-linked provenance assertions. | Prove new fields are supported and unsupported claims remain rejected. | `4b2d135` |
| `docs/ASSET_STATE_DESIGN.md`, `docs/CANONICAL_DATA_MODEL.md`, `docs/PHASE1_STATUS.md`, `KNOWN_ISSUES.md`, `docs/CHANGE_COORDINATION.md` | Recorded approved G3 decisions, current semantics, evidence, limitations, and corrected `LOST` terminology. | Keep implementation and operator-facing design records aligned. | This documentation commit |
| `KNOWN_ISSUES.md`, `docs/PHASE1_STATUS.md`, `docs/CHANGE_COORDINATION.md` | Added fresh-HEAD G3 regression comparison, H0-to-012 rehearsal counts, and the observed Ollama endpoint/model plus untested pull command. | Record reproducible evidence without editing Amr's DAY3 reports or changing deployment behavior. | This documentation commit |
| `docs/INCIDENTS.md`, `docs/CHANGE_COORDINATION.md` | Recorded the 2026-11-23 migration-target incident, the operator's keep decision, and mandatory disposable-container DB command checks. | Preserve the verified incident facts and prevent host-port misdirection in future rehearsals. | Pending |
| `db/migrations/013_building_code_required.sql`, `scripts/verify_schema.sql` | Added NOT NULL and full unique building-code enforcement, an actionable preflight error, and schema/ledger checks. | Make building identity administrator-supplied and prevent ambiguous mappings. | `3888154` |
| `tests/integration/test_multibuilding_g4.py` | Added disposable multi-building end-to-end proof for source-key isolation, mixed record ingestion, keepalive, liveness, freshness, gap, G3 state, fixed-window A parity, and a timezone distinct from the seed site. | Verify the existing contract and evaluators across two buildings without changing application code/API shape. | `e5606ae`, `e5ddcb5` |
| `deploy/examples/g4_second_building.sql`, `docs/PHASE1_STATUS.md`, `KNOWN_ISSUES.md`, `docs/CHANGE_COORDINATION.md` | Added second-site SQL example, G4 evidence/status, exact pre-existing-database migration step and remaining dashboard/catalogue limitations. | Make the verified G4 setup repeatable and record the missing public building filter without expanding API scope. | This documentation commit |
| `app/dashboard/static/dashboard.html` | Added an optional "All buildings" selector, a `buildingFilter` state, and an `apiPath()` helper that appends `building`; alert and incident loads honour it. No existing markup renamed or removed. **Amr, please review: this file is shared.** | Let operators scope the dashboard to one building. | pending commit |
| `app/main.py`, `app/domain/incidents/incident_engine.py` | `GET /incidents` accepts an optional `building`; `list_incidents` takes an optional `building_code`. Unknown code => 400. | Building-scoped incident list; omitted => unchanged. | pending commit |
| `app/api/frontend_api.py`, `app/api/freshness_api.py`, `app/repositories/dashboard_core_pg.py` | Optional `building` on overview/trends/equipment-health/alerts and the three freshness endpoints; `/dashboard/filters` returns an additional `building_code`. | Building-scoped reads; additive only. | pending commit |
| `tests/unit/test_g4_safety.py`, `docs/G4_MULTI_BUILDING.md` | New no-database safety tests and the G4 verification/hardcoded-dependency note. | Prove a pre-012 database degrades safely and `_g3_evidence` stays private. | pending commit |
