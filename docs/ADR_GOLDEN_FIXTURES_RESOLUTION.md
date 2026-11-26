# ADR / golden fixtures / implementation resolution (2026-11-26)

Status: uncommitted continuation. **No commit or push was performed.** Tasks 5-7 and `maintenance.candidates` were not touched
(`maintenance_candidates.py`, its tests and fixture are unchanged; no migration was added).

## 1. Conflict matrix

| # | Scenario / rule | ADR | Golden fixture | Implementation | DB vocabulary | Finding |
|---|---|---|---|---|---|---|
| 1 | H1 coverage ends before period end | D-09/D-12 did not say what is claimed at the period end | `events.json` 0.2: partial coverage; `expectations.json` 0.1: `COMPLETE`, `complete: true` | `PARTIAL` (correct) but no period-end state; `open_at_period_end` = true read as "open at the end" | n/a | `FIXTURE_CONFLICT` (stale oracle) + `IMPLEMENTATION_BUG` (no UNKNOWN end state). **Fixed.** |
| 2 | C1 duplicate delivery | D-04 | S-C1 | collapses `(source_id, source_event_id)` | `uq_event_source_event_id` (006) | `CONSISTENT` |
| 3 | C2 legacy NULL identity | D-04 silent on the legacy key limit | S-C2 (rows 1 s apart) | counted individually, flagged `MISSING_EVENT_IDENTITY` | `uq_event_legacy_natural_key (asset,type,ts)` (007): two real events at one instant cannot both be stored | `ADR_CONFLICT` (incomplete). **Documented** in D-04. |
| 4 | READY_FOR_CORE | no definition | `status_mirror: READY_FOR_CORE` on 8 scenarios while `events.json` said `CONTRACT_SHAPE_ONLY_REPO_UNVERIFIED` | no vendor connector, empty `DEFAULT_REGISTRY`, no `CoverageProvider` | only `connectors/reference_simulator` | `FIXTURE_CONFLICT`. **Relabelled**; ADR section 7 separates prototype replay from real connector readiness. |
| 5 | TZ / DST rules | D-10 consistent with `periods.py` | S-T1 ok; S-T2 expects tzdb 2026f (Havana 24 h) | rules correct; 2 tests read the real Havana zone and fail on a host tzdb older than 2026f | n/a | `FIXTURE_CONFLICT` (host-tzdb dependence; scenario id "REPEATED" no longer describes a repeat under 2026f). **Tests made host-independent.** |
| 6 | Outage ranking metric | open question c "Proposed" | meta 0.2: `outages_started_in_period` (team confirmation) | default `outages_started` -> `outages_started_in_period` | n/a | `ADR_CONFLICT` (stale). **D-13 added**; ongoing-at-start ranking `NEEDS_DECISION`. |
| 7 | Forced open counting (D-01) | counted if source confirms physical opening | S-E1 count 1 with 2 refs, S-E2 count 2, both `COMPLETE` | `door_forced` never counted, result `PARTIAL` + `CORRELATION_UNAVAILABLE` | `door_forced` (ACTIVITY); no `door_forced_open` | `ADR_CONFLICT` + `FIXTURE_CONFLICT`; implementation matches the approved rule. **ADR and fixtures fixed.** |
| 8 | Catalogue vocabulary after 014 | "catalogue has no `door_opened`" | `BLOCKED_BY_CATALOGUE`, `CATALOGUE_NO_DOOR_OPENED`, mappings with `core_event_type: null` | reads `door_opened` | `door_opened`, `access_granted` added (ACTIVITY, ACCESS_CONTROL) | `ADR_CONFLICT` + `FIXTURE_CONFLICT` (both stale); DB `CONSISTENT`. **Updated.** |
| 9 | `ACCESS_GRANTED` / `UNLOCK_COMMAND` | not openings | S-D: 0 rows | not read | `access_granted` exists; no `unlock_command` | `CONSISTENT` |
| 10 | `resolves_event_type` pairs | glossary | `event_catalog_014.json` | legacy lists unchanged | `link_up->link_down`, `camera_online->camera_offline`, `alarm_cleared->fire_alarm_triggered`, `hvac_recovered->hvac_fault`, same `outage_class` | `CONSISTENT` by static comparison of migration SQL, JSON and code lists (parity tests could not be executed here, see 4) |
| 11 | `event_class` vocabularies | n/a | n/a | `integrity_rules.EVENT_CATALOG` uses FAULT/STATE_OPEN/STATE_CLOSE; `door_forced` is FAULT there, ACTIVITY in 014 | 014: ACTIVITY/FAULT_OPEN/FAULT_CLOSE/HEARTBEAT/STATE_REPORT | `NEEDS_DECISION` (two coexisting vocabularies; not changed) |
| 12 | S-I evidence list | n/a | lists EV-I-001 | look-back keeps only EV-I-002 | n/a | `NEEDS_DECISION` (cosmetic), both sides asserted |
| 13 | S-M cross-building ranking | D-13 | one ranking over two buildings | tool takes one building | n/a | `NEEDS_DECISION` |
| 14 | S-N missing/untrusted timestamps | D-11 / open question f | expects `PARTIAL` | no quality field, no quarantine path -> `COMPLETE` | none | `DATABASE_CONFLICT` (unrepresentable); unchanged, `BLOCKED_BY_SCHEMA` |

## 2. Decisions made

1. H1: `state_at_period_end` is known only if trusted coverage is confirmed for the whole period and the cutoff is the period end; otherwise `UNKNOWN`. Query time is never coverage.
2. `open_at_period_end` is kept (compatibility, no existing assertion weakened) and documented as "no recovery observed up to the cutoff".
3. Forced open is not an opening (D-01); no link without a contract field.
4. Legacy-row identity limits are documented (D-04). Nothing is merged by similarity.
5. Ranking: D-13, `outages_started_in_period`, downtime as explicit alternative.
6. Readiness: prototype replay is not connector readiness.

## 3. Files changed

- `app/domain/tools/devices_offline_frequency.py`: additive `state_at_period_end` column/field and `DeviceAnalysis.final_state`. No SQL, signature, registry or envelope-enum change.
- `docs/ADR-0001-analytics-semantics.md`: amendment A1 (D-01, D-04, D-09, D-10, D-13, glossary table, schema, follow-ups, open questions, section 7).
- `tests/fixtures/analytics/events.json`, `expectations.json`: v0.3 (S-E1, S-E2, S-H1, S-H2, status vocabulary, door mappings, resolved dependencies). Only values that contradicted an approved rule changed.
- `tests/unit/test_devices_offline_frequency.py`, `tests/unit/test_doors_after_hours.py`: see `docs/CHANGE_COORDINATION.md`.
- `docs/DOORS_AFTER_HOURS.md`, `docs/DEVICES_OFFLINE_FREQUENCY.md`, `docs/CHANGE_COORDINATION.md`, this file.

## 4. Tests (this environment: Python 3.12.3, no pytest, no PostgreSQL, no Docker, no network)

Commands were run from the repo root with `python3 -m unittest tests.unit.<module>`.

| Suite | Before (original tree) | After |
|---|---|---|
| `test_doors_after_hours` | 34 passed | 35 passed, 0 failed, 0 skipped |
| `test_devices_offline_frequency` | 81 passed, **2 failed** (Havana, host tzdb) | 98 passed, 0 failed, **2 skipped** (host tzdb older than 2026f) |
| `test_tool_envelope` | n/a | 23 passed |
| `test_evidence_validation` | n/a | 7 passed |
| `test_asset_state` | n/a | 4 passed |

The 9 new period-end-state/H1/H2 checks fail against the original `devices_offline_frequency.py` and pass now. Fixture JSON parses.

**Not executed:** all PostgreSQL integration tests (`test_event_catalogue_migration`, `test_doors_after_hours_sql`, `test_devices_offline_frequency_sql`, `test_envelope_evidence_validation`, `test_maintenance_candidates_sql`), `test_event_catalog_parity` (needs psycopg), `test_maintenance_candidates` (needs pytest), `test_freshness` (import error, same on the original tree), the Day 2 / Frontend / chat-consistency harnesses, and the full application regression. No PostgreSQL version applies. They must be run on a disposable Compose project, e.g. `docker compose -p anzma-adr-resolution-<date> ...` with no host DB port, as in `docs/TASK7_HANDOVER.md`.

## 5. NEEDS_DECISION (human approval)

1. Confirm D-13 as the approved ranking metric (the only evidence is the fixture 0.2 note) and how ongoing-at-start sessions rank.
2. Rename or deprecate `open_at_period_end` in favour of `state_at_period_end`.
3. Count confirmed forced openings in a separate metric? Needs a contract field.
4. Early-hours (before opening) limitation note (open question a).
5. Two `event_class` vocabularies (row 11); S-I evidence granularity; S-M cross-building ranking.
6. Verify tzdb 2026f against an authoritative source; the fixture note is the only evidence here.
7. Rename S-T2 (it is no longer a repeated-midnight case under 2026f).

## 6. Remaining dependencies on Amr

Connector retrieval-completion and coverage reporting (follow-up 6) and a production `CoverageProvider`; a contract correlation or physical-open field (follow-up 4); vendor-to-catalogue mapping in real connectors; registering the tools in `DEFAULT_REGISTRY` and Chat integration; running the PostgreSQL suites.
