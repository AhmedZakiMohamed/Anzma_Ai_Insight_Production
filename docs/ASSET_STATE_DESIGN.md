# G3: Core-derived asset state

**Phase 1 item:** 1.4
**Status:** APPROVED; shadow implementation complete.
**Decision recorded:** 2026-11-22.
**Approver:** project owner (name not recorded).

## Current behavior verified in the repository

- Connector-reported equipment state is stored as evidence in `equipment_state_records`; `v_asset_current_state` is a latest-report view, not Core-derived health (`db/migrations/006_ingestion_foundation.sql`).
- The dashboard's operational bucket remains computed by `app/repositories/dashboard_core_pg.py::asset_status_rows` using its existing alert/event/data-window rules. G3 reads that result; it does not replace dashboard, alert, incident, or chat readers.
- Freshness and gaps are persisted by `app/services/freshness.py`; source liveness is persisted by the same job. Contract §9 keeps source liveness and gap causes evidence-only. `v_telemetry_valid` excludes `BAD`, while `UNCERTAIN` and legacy `NULL` quality remain visible for compatibility.

## Approved decisions and implemented mapping

The evaluator is a shadow projection. It runs after freshness under the freshness job's process-wide advisory lock, then uses its own database transaction and a separate state-stage lock. A state-stage failure rolls back only that stage; the last committed state snapshot remains, freshness stays committed, and the job emits `ASSET_STATE_PASS_FAILED`. It never writes a default healthy or offline state.

| Existing dashboard bucket | `operational_state` | `availability` | `communication_status` |
|---|---|---|---|
| `critical` | `CRITICAL` | Latest applicable Core event evidence (`camera_offline`/`link_down` → `UNAVAILABLE`; `camera_online`/`link_up` → `AVAILABLE`); otherwise `UNKNOWN` | Derived independently from per-series freshness and source attribution below |
| `warning` | `WARNING` | Same Core-event rule | Same freshness rule |
| `normal` | `NORMAL` | Same Core-event rule | Same freshness rule |
| `no_data` | `UNKNOWN` | Same Core-event rule | Same freshness rule |
| `unknown` or unrecognized | `UNKNOWN` | Same Core-event rule | Same freshness rule |

For communication status, an asset attributed to multiple sources, or a mixture of sourced and unattributed series, is `UNKNOWN` with the source evidence retained. For an unambiguous single source: any `MISSING` series yields `SILENT`; otherwise any `STALE` series yields `DEGRADED`; all `FRESH` series yield `COMMUNICATING`; no usable freshness evidence yields `UNKNOWN`. `NOT_APPLICABLE` rows do not determine the result. Source liveness (`ALIVE`/`SILENT`) and each series freshness remain separate in the evidence summary. Communication is never labelled `OFFLINE` or `UNAVAILABLE`.

Connector-reported `availability` is evidence only and never sets Core availability. Core availability uses the existing Core event evidence. Silence, gap cause, stale/missing freshness, and connector status never change operational health or create alerts.

The first successful evaluation writes one `BASELINE` history row at its actual UTC evaluation time. Later history rows are appended only when the semantic tuple `(operational_state, availability, communication_status)` changes. Freshness-only changes update the current evidence summary but do not add history transitions. `state_since` changes only with that semantic tuple.

`UNCERTAIN` is included as valid evidence and marked in the private data-path context. It lowers `data_sufficiency` to `partial` only when a citation in the final explanation intersects the reference of an `UNCERTAIN` telemetry measurement. It does not downgrade grounding validation by itself. `BAD` is excluded through `v_telemetry_valid`; legacy `NULL` quality/provenance is disclosed and never backfilled.

## Storage and migration

Migration `012_asset_state_shadow.sql` is additive and idempotent. It creates `asset_state_current` and append-only `asset_state_history`, with restrictive asset foreign keys, semantic checks, indexes, JSONB `evidence_summary`, and `evidence_schema_version`. A trigger rejects history `UPDATE` and `DELETE`. It records `012_asset_state_shadow` in `schema_migrations`; no old fact or historical row is rewritten. `scripts/verify_schema.sql` checks every G3 table, column, index, trigger, function, and ledger entry.

For an already-running database, after reviewing and verifying a backup, the operator applies pending migrations through the existing runner from the intended Compose project:

```sh
docker compose --env-file .env -p "$PROJECT" -f deploy/compose/docker-compose.yml exec -T api python scripts/apply_migrations.py
```

Set `PROJECT` to the intended database's Compose project. Fresh-volume Compose initialization is separate; it does not apply migrations to an existing volume. The migration was applied and verified only in the disposable project `anzma-taska-current-20261122` for this work.

## Internal evidence and compatibility

`pipeline.explain_alert` attaches a private, versioned `_g3_evidence` object assembled from the current shadow snapshot and citation-linked fact rows. It includes state, relevant freshness, source liveness, quality, provenance, and legacy/unattributed markers. Grounding recognizes the new evidence values; unsupported numeric claims remain rejected. This object is not added to a public API response.

Canonical sorted-JSON SHA-256 comparison used the same seeded DB and same request against source at `1737e06` and the G3 working code. Both sides returned HTTP 200 and identical hashes:

| Surface | SHA-256 | Canonical bytes |
|---|---|---:|
| `GET /dashboard/overview` | `81ba2aa60141a2191ddd61f8d9663b6a7ad1ecf01108c31af856ec2153ab4dce` | 4383 |
| `GET /alerts` | `62c28825cda4c9a89fb213f195c45554febf608e4f348c862137346034de392b` | 4671 |
| `GET /incidents` | `f322be7f98021b9dea5f3a58ed8d2b5c5134df96656511cc1f49ffa04da2dcf9` | 2724 |
| `POST /chat` (`What is the current building status?`, 24 h, deterministic) | `a710019ba566f92cbf78abc7b81257db69a07e123c8c3c79df8fb395022a8334` | 2137 |

Parity command `python scripts/state_parity_report.py --db anzma` reported 12 seeded assets and 0 mismatches. `python scripts/state_parity_report.py --db anzma_g3_parity --scenarios` reported those 12 plus `fault_open`, `fault_recovered`, `resolved_alert`, `stale_series`, `silent_source`, `uncertain_only`, and `null_legacy_quality`, with 0 mismatches. Synthetic rows were rolled back to savepoints. The parity tool is CLI-only.

## Verification and limitations

On the disposable PostgreSQL 16 project, G3 integration tests passed 6/6 twice; the grounding unit tests passed 3/3; full unit suite passed 83 tests (1 skipped); the migration runner skipped 006–012 on two repeated runs because they were already recorded; `verify_schema.sql` returned `OVERALL OK`. Other Phase 0/AI regression results are recorded in `docs/PHASE1_STATUS.md`.

The projection is not an operational authority and has no public API/dashboard reader. It covers the existing dashboard bucket mapping, not an independently redesigned fault model. Legacy NULL provenance remains ambiguous. `asset_state_history` grows without bound until G5 defines and approves a retention policy; no history deletion is authorized by G3. These limitations must remain in the Phase 1 limitations list.
