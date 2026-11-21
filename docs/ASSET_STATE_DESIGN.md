# G3 design: Core-derived asset state

**Phase 1 item:** 1.4, G3

**Status:** proposal for human review; no schema, code, or response changes are authorized by this note.
**Decision: PENDING — approval is required before implementation.**

## 1. Verified current behavior

- `equipment_state_records` stores source-reported `reported_state` (`RUNNING`, `STOPPED`, `STANDBY`, `FAULT`, `UNKNOWN`) and `availability` (`AVAILABLE`, `UNAVAILABLE`, `UNKNOWN`) as append-only evidence, along with source, asset, observed/received timestamps, reason, message, batch and metadata. Its natural uniqueness is `(source_id, asset_id, observed_at)`. See `db/migrations/006_ingestion_foundation.sql:140-159`.
- `v_asset_current_state` selects the latest connector report by `observed_at`, then `state_id`; it is not a Core-derived health view. See `db/migrations/006_ingestion_foundation.sql:161-165`.
- `v_telemetry_valid` excludes `BAD` only. Thus `UNCERTAIN` and legacy `NULL` quality rows are included by this compatibility view. See `db/migrations/006_ingestion_foundation.sql:167-168` and contract §7.
- The E3 job persists series freshness in `series_freshness_current`; E4a persists missing-series gaps; E4b persists `source_liveness_current`, transitions and gap-cause audit. Their current semantics are distinct: freshness is per expected series; source liveness and gap cause are evidence only; they do not change asset health or create alerts. See migrations 009-011 and contract §9.
- Existing dashboard health is computed by `app/repositories/dashboard_core_pg.py::_asset_status_counts` (around lines 437-454 onward). It gives priority to active critical/warning fault evidence and alerts, then `no_data`, `unknown`, and `normal`; resolved alert lifecycle affects fault interpretation. This query is an existing compatibility surface.
- Alert input is assembled by `app/adapters/database/db_adapter.py::load_context` (around lines 100-154). Events include event source rows; telemetry selects value and timestamp through `v_telemetry_valid` but does not select its `quality` or `source_id`. `app/domain/alerts/alert_engine.py::make_alert` (around lines 169-183) stores thresholds, facts and selected event evidence. The event evidence formatter includes a hard reference to an event or telemetry source row (`evidence_event`, around lines 134-141).
- `app/domain/evidence/validation.py::assess_evidence` (around lines 46-59) grades evidence presence and supported causes, and the response includes `data_sufficiency`, `validation`, and `limitations` (around lines 83-93). `freshness_limitation` currently describes the latest stored record time (around lines 102-107). Grounding validation checks supported facts/recommendations/references; it does not establish that evidence is complete or that an equipment state is semantically true.
- Current contract §9 defines series freshness (`FRESH`/`STALE`/`MISSING`/`UNKNOWN` and event-only `NOT_APPLICABLE`) and E4b source liveness (`ALIVE`/`SILENT`/`UNKNOWN`/`DISABLED`). It explicitly states that source liveness and gap causes are evidence only and never change asset health, create `OFFLINE`/`UNAVAILABLE`, or generate alerts. Contract §7 defines `UNCERTAIN` as valid and included by default, with uncertainty marked in evidence; `BAD` is excluded from operational reads.

## 2. Proposed state model (not implemented)

Keep four concepts separate: (1) raw facts, (2) per-series freshness, (3) per-source liveness, and (4) an asset-level Core projection. Do not turn absence into an equipment fault.

| Proposed object | Grain and purpose | Proposed constraints / lifecycle |
|---|---|---|
| `asset_state_current` | One current projection per asset: `operational_state`, `availability`, `communication_status`, `evaluated_at`, `state_since`, reason/evidence references, and a compact evidence/sufficiency summary. Per-series freshness remains in `series_freshness_current`; do not collapse all series into one freshness value without an approved rule. | `asset_id` primary key/FK; enumerated values; timestamps UTC; upsert in the evaluator transaction. Keep connector `reported_state` in its fact table, not as the authoritative value here. |
| `asset_state_history` | Append-only transitions of the approved asset-level semantic tuple, with old/new values, UTC `evaluated_at`, causal event/fact reference where known, and source/freshness evidence references. | FK references use restrictive deletion. Write only when an approved semantic value changes; a repeated evaluation must not add duplicate transitions. A DB trigger/constraint should reject edits/deletes after insertion. |

**Additive migration proposal:** next migration is `012_asset_state.sql`, subject to migration-runner review and human approval. It would create only new tables/indexes/constraints and record the migration in the existing ledger. No committed migration is edited; no existing fact row is updated or deleted. Do not backfill fabricated history. The first successful evaluator pass may create a current snapshot from available evidence; whether to create an initial history row is a decision below. Existing rows with NULL provenance or quality remain unchanged.

## 3. Proposed derivation rules and precedence

These are proposals requiring approval. Current `CANONICAL_DATA_MODEL.md` §8 already contains a proposed four-dimension model, but its text is older than the approved E4b contract rules and includes status wording that needs reconciliation (see decisions below).

| Dimension | Candidate rule | Guardrail |
|---|---|---|
| `operational_state` | Reuse the currently implemented Core fault/alert lifecycle logic: unresolved opener/recovery event pairs and active alerts determine `CRITICAL`/`WARNING`; otherwise `NORMAL` when sufficient evidence exists, else `UNKNOWN`. The current pair list is `FAULT_RECOVERY_PAIRS` in `app/repositories/dashboard_core_pg.py`; the current query also considers unpaired event severity and resolved alert spans. | Connector `reported_state` never directly assigns or overrides this Core dimension. Silence, source `SILENT`, gap cause, `STALE`, and `MISSING` never create an operational fault or `OFFLINE`. Exact event-to-state precedence and treatment of conflicting active alerts need approval and parity tests. |
| `availability` | Derive from explicit Core event evidence and, only if approved, validated connector availability reports. | Never infer `UNAVAILABLE` from missing telemetry, source silence, a gap, or a missing state report. Decide whether and how `equipment_state_records.availability` is trusted, reconciled, and superseded by Core events. |
| `communication_status` | A separate data-path projection informed by per-series freshness and source liveness, without changing operational state or availability. Preserve source liveness values as their own diagnostic dimension. | Do not use older `LOST` wording until reconciled with E4b’s `SILENT` state. No communication label may be rendered as equipment `OFFLINE` or `UNAVAILABLE`. Exact multi-source aggregation needs approval. |
| `UNKNOWN` / evidence sufficiency | Distinguish “no known operational facts” from an observed normal state. Missing source/asset mapping or legacy NULL provenance makes attribution incomplete, not proof of a fault. | Existing dashboard bucket parity must be measured before replacing any current counts. |

**Quality and provenance:** carry `quality`, `source_id`, observation time, receive time and source-row reference into alert evidence when available. `UNCERTAIN` records remain eligible under §7 but are visibly marked and may lower evidence sufficiency; they must not be silently treated as `GOOD`. `BAD` remains excluded through `v_telemetry_valid`. A NULL legacy quality remains unknown in storage; do not backfill it to `GOOD`. Source liveness (`last_received_at`) remains separate from measurement success (`last_success_at` / error evidence), as contract §9 specifies.

**Freshness and gaps:** retain the full per-series snapshot and gap state as evidence. One stale or missing metric does not by itself set asset operational state to warning/critical or availability unavailable. Whether it changes the asset-level communication projection, and how multiple sources serving one asset are aggregated, are human decisions. A gap cause (`UNKNOWN`, `NO_DATA`, `SOURCE_SUSPECTED`) is explanatory evidence only.

**Evaluation proposal:** extend the existing freshness evaluator/job pattern after the freshness/source/gap snapshot is computed, using one UTC evaluation timestamp and a consistent DB transaction for each asset-state snapshot. Locking, retries and idempotency should follow G2’s single-instance job pattern. History is written only for semantic transitions, not every periodic timestamp refresh. Job failure must leave the last committed current snapshot intact and emit an operational error; it must not manufacture a default healthy or offline state.

## 4. Alert and AI evidence (proposed, no output change approved)

- Enrich internal alert evidence with the current state snapshot, relevant freshness rows, source liveness evidence, `UNCERTAIN` flags, data age, provenance references, and a clear indication when values are legacy/unattributed. Keep the existing raw event/telemetry references and alert facts.
- Add a data-sufficiency assessment separate from grounding validation. Example limitation categories to decide: no current freshness snapshot, stale snapshot, missing expected series, source unknown/silent, uncertain-only measurements, truncated evidence, and legacy rows without source/received metadata. Do not claim “complete” merely because grounding checks passed.
- AI explanations should state material evidence limitations in the returned limitation text when relevant, and must not infer a fault from silence or report a connector-reported state as Core-validated health. The AI fallback and grounding-validation contract remain intact.
- Do not change `/alerts`, chat, or explanation response shapes in this design phase. Adding fields to alert evidence or API responses is a contract/interface change and needs explicit approval plus contract documentation before implementation. Recommendation: initially add an optional versioned internal evidence object, prove existing response parity, then propose any public exposure separately.

## 5. Compatibility and parity

Keep existing dashboard status counts, alert detections, incidents, and response shapes unchanged in the first implementation. Run the existing seeded-data KPI/alert/incident checks before and after on a disposable DB and compare canonical outputs. If the proposed projection disagrees with existing behavior, report the exact rows/rules and stop for a decision; do not silently alter legacy semantics. A later approved migration of consumers should be a separate step.

## 6. Safe implementation outline

1. After approval, implement migration 012 additively and extend `scripts/verify_schema.sql` for every new object.
2. Add a pure derivation function with injected evaluation time and explicit evidence inputs; unit-test precedence and “silence never offline/unavailable.”
3. Add an idempotent persistence/evaluator path and disposable-DB tests for current-state upsert, transition-only history, replay, concurrent/repeated passes, NULL legacy provenance/quality, `UNCERTAIN`, and `BAD` exclusion.
4. Only after those checks, enrich alert evidence internally and update AI limitation wording. Any externally visible shape change gets a separate contract/design approval.

No migration, implementation, disposable DB, API change, or test suite was run for this proposal. All proposed schema and behavior above are **NOT TESTED**.

## 7. Decisions needed from the human

1. **Approve this design to proceed with migration 012 and implementation?** Recommendation: approve only the additive state tables and shadow evaluator first; keep existing readers authoritative until parity evidence is reviewed.
2. **Operational-state semantics:** approve reuse of the current alert/event pairing and severity behavior, including the exact reset/recovery and conflict precedence? Recommendation: preserve current health behavior byte-for-byte initially and expose the projection in shadow mode.
3. **Availability evidence:** may a connector’s explicit `availability` field ever set Core `UNAVAILABLE`, and how does it interact with Core event evidence? Recommendation: retain it as evidence only until trust/precedence rules are explicit; only existing Core events drive current availability.
4. **Communication labels:** how should `ALIVE`/`SILENT` and series `FRESH`/`STALE`/`MISSING` map to an asset-level communication status, especially across multiple sources? Recommendation: keep source and series values independently visible; use `UNKNOWN` where aggregation is ambiguous; do not use `LOST` until canonical wording is reconciled.
5. **History:** should the first evaluator pass create a baseline history row, and should freshness-only changes append history? Recommendation: baseline current row without historical transition; append only semantic asset-level tuple changes and keep per-series freshness in its own table.
6. **UNCERTAIN sufficiency:** should any `UNCERTAIN` supporting measurement force `PARTIAL`, or only be disclosed as a limitation? Recommendation: disclose it and mark evidence; only lower sufficiency when the explanation depends on it, with deterministic tests.
7. **Output surface:** which API/chat/alert consumers may receive the new state and evidence fields? Recommendation: no public response change in initial G3; seek separate approval after shadow results.

**Decision: PENDING.**
