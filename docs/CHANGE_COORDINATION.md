# Change coordination

This note defines ownership and records cross-owner changes. Preserve each owner's existing behavior; make small additive patches and record review-sensitive touches here.

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

## Change log

| File | What changed | Why | Commit |
|---|---|---|---|
| `scripts/backup_db.sh` | Made retention mtime lookup GNU-first, BSD-fallback, numeric-only; invalid values warn and skip. | Fix Linux retention failure without deleting files on invalid metadata. | `18ac4d0` |
| `tests/unit/test_backup_retention.py` | Added malformed-mtime regression coverage; existing assertions retained. | Prove malformed metadata is safely skipped. | `18ac4d0` |
| `tests/integration/test_frontend_api.py` | Amr's test updated to match dashboard limit 200, please review. | Match the deliberate `6f190e8` dashboard request while keeping the assertion's purpose unchanged. | `0476405` |
| `tests/integration/test_freshness_job.py`, `test_freshness_gaps.py`, `test_source_liveness.py` | Moved injected test time just after test start. | Keep fixtures later than migration-created default validity timestamps. | `76f0169` |
| `docs/BACKUP.md` | Documented the prior Linux retention defect. | Warn operators about earlier script versions. | `dfc8ddf` |
| `docs/CHANGE_COORDINATION.md`, `docs/PHASE1_STATUS.md` | Added ownership, interface, migration, Amr-overlap and baseline records. | Coordinate continuation from main and preserve evidence. | This documentation commit |
