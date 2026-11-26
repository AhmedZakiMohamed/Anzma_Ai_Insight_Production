# doors.after_hours (design note and fixture review)

- **Code:** `app/domain/tools/{doors_after_hours,periods,coverage}.py` (envelope/registry: `docs/TOOL_REGISTRY_DESIGN.md`).
- **Tests:** `tests/unit/test_doors_after_hours.py` (no DB, includes golden-fixture replay) and
  `tests/integration/test_doors_after_hours_sql.py` (disposable PostgreSQL; **written but not yet executed**, no server was available).
- **Not wired:** `DEFAULT_REGISTRY` is still empty. Registering needs a read-only dict-row cursor factory and, later, a
  coverage provider; see "Wiring" below.

## Decisions implemented

| # | Decision | Implementation |
|---|---|---|
| 1 | Only `door_opened` is a physical opening | SQL reads `door_opened` and `door_forced`; only `door_opened` is counted. `door_forced` is never counted and never linked. If any `door_forced` event falls inside the after-hours windows the result is **`PARTIAL`** (never `COMPLETE`) with `CORRELATION_UNAVAILABLE`, because the opening count may be an underestimate; per-door `uncounted_door_forced_events` and the limitation's `details.uncounted_door_forced_events` keep the evidence. `door_forced` outside the windows (e.g. during working hours) cannot affect an after-hours count and does not downgrade. It cannot turn `UNSUPPORTED` / `CANNOT_COMPUTE` into `PARTIAL`. |
| 2 | No assumed `correlation_id` | Checked `db/init`, migrations 003-014 and `docs/INGESTION_CONTRACT.md`: no correlation column or contract field exists (only `docs/data-model.sql`, which is not the live schema, and fixtures). Ignored. When `door_forced` events fall in the windows they are reported (`uncounted_door_forced_events`) with limitation `CORRELATION_UNAVAILABLE`. |
| 3 | `PARTIAL` + `COMPLETION_NOT_CONFIRMED` without evidence | `coverage.assess_coverage`. `COMPLETE` needs a `CoverageProvider` supplying, per door source, `supports_history_events`, `retrieval_confirmed_complete` and a confirmed range covering the whole period. The provider is injected when the ToolSpec is built, so it is not a tool argument: unknown arguments such as `complete` are rejected as `CANNOT_COMPUTE` (tested). A source without `history.events` gives `CAPABILITY_UNSUPPORTED`; all sources unsupported gives `UNSUPPORTED`. |
| 4 | Reuse shared time/DST/dedup code | None existed: `alert_engine` only converts an instant to a local hour, migration 010 hard-codes `Etc/GMT-2`. Deduplication already exists as the unique index `(source_id, source_event_id)` (migration 006) plus the legacy natural-key index (007); the tool collapses same-identity duplicates defensively and flags events without identity. New minimal module `periods.py` covers only period parsing, D-10 midnight rules and after-hours windows; `devices.offline_frequency` should reuse it. |
| 5 | `Etc/GMT-2` provenance | Migration 009 sets `timezone = COALESCE(timezone, 'Etc/GMT-2')` and stores no marker, and `scripts/freshness_admin.py set-schedule` can store the same value explicitly, so the two cannot be told apart. A building whose timezone is `Etc/GMT-2` is therefore `PARTIAL` + `TIMEZONE_PROVENANCE_UNCONFIRMED` unless trusted wiring lists its code in `confirmed_timezone_buildings`. Any other value cannot come from the backfill and is treated as explicit. The limitation also notes the zone has no DST rules. NULL, empty or invalid timezones give `CANNOT_COMPUTE` / `TIMEZONE_UNAVAILABLE`. |

Other behaviour: `start`/`end` are building-local dates (end inclusive) or timestamps with an explicit offset (end exclusive);
windows are `[after_local, next local midnight)` per local day, half-open, clipped to the period, at most 366 days;
a nonexistent or ambiguous `after_local` is an error, not a guess. Rows are produced only for doors with at least one counted
opening, grouped by (asset, source), ordered by count desc then identity, each with `evidence_refs` = `event_record` ids.

## Golden fixtures (resolved in fixture 0.3; see `docs/ADR_GOLDEN_FIXTURES_RESOLUTION.md`)

1. **S-E1 / S-E2 (forced open).** ADR D-01 was amended: `DOOR_FORCED_OPEN` (`door_forced`) is not counted as a normal opening and is never linked (no correlation field in contract 1.0). The fixtures now expect E1 = 1 opening (ref V-E1-01), E2 = 1 opening, both `PARTIAL` with `CORRELATION_UNAVAILABLE` and `uncounted_door_forced_events` = 1. The unit tests assert the fixtures directly.
2. **`COMPLETE` expectations (S-A, S-B, S-D).** The replay now injects a fixture-backed `CoverageProvider`, so `COMPLETE` is reproduced and asserted. Without a provider the tool is still `PARTIAL` (tested separately).
3. **Early-hours note.** ADR open question (a) recommends a limitation note about hours before opening; fixtures expect `limitations: []`; not emitted (`EARLY_HOURS_NOT_COVERED` is unused). Unchanged, **NEEDS_DECISION** in the ADR.
4. **Evidence ids.** Fixtures use vendor fixture ids; the tool returns `event_records.event_id` (naming only).
5. **`excluded_evidence_refs`** are not returned: excluded events are filtered in SQL.

## Known limitations

- Timestamp quality (D-11): `event_records` has no timestamp-quality field in contract 1.0, so untrusted timestamps cannot be excluded.
- A door with several external ids for one source shows them joined with `, ` in `door_external_id`.
- All qualifying events in the windows are fetched and counted in Python (needed for evidence refs); 366 days of one building's door events is assumed small. Apply `statement_timeout` in the cursor factory.
- "Within the requested period" is implemented as "within the after-hours windows of the period", the only place a forced event can affect this tool's count.

## Running the PostgreSQL integration test

See the commands in the task report; short form: start a **disposable** compose project (own volume), then run
`python -m unittest tests.integration.test_doors_after_hours_sql -v` inside the `api` image with the working tree mounted.
The compose DB init only mounts migrations up to 012, so the test builds its own throw-away database, applies 013/014 through
`scripts/apply_migrations.py` (which assigns the codes `MAIN-TOWER` / `DATA-CENTER-ANNEX`) and drops it afterwards.

## Wiring (for the owner of chat / app startup)

```python
spec = build_doors_after_hours_spec(cursor_factory,            # context manager -> dict-row cursor, READ ONLY transaction
                                    coverage_provider=None,    # until connectors report coverage
                                    confirmed_timezone_buildings=frozenset())
DEFAULT_REGISTRY.register(spec)
```
