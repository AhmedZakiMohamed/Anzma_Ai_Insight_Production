# ADR-0001: Analytics semantics for doors and offline devices

- **File:** docs/ADR-0001-analytics-semantics.md
- **Status:** Accepted (team decision, recorded on the date of writing, 2026-11-24)
- **Authors:** Ahmed Zaki (decisions); Amr Eldesouky (reviewer)
- **Amendment A1 (2026-11-26):** aligns D-01, D-04, D-09, D-10, the glossary, the result schema and the open questions with migration 014 and the implemented tools; adds D-13 and section 7 (readiness levels). Evidence and per-item status: `docs/ADR_GOLDEN_FIXTURES_RESOLUTION.md`. Anything marked **NEEDS_DECISION** is not decided.
- **Related documents:** INGESTION_CONTRACT.md (contract 1.0); CANONICAL_DATA_MODEL.md; CEO's v0.2 specification
- **Convention:** Everything in D-01 to D-13 is **decided** (D-13 and the amendments carry the caveats stated in them). Anything else in this document is labelled **Proposed** or **Open question**.

---

## 1. Context

The v0.2 specification requires deterministic analytics tools (`doors.after_hours`, `devices.offline_frequency`) that feed chat, reports and dashboards. The principle is: **deterministic, validated code computes; the AI only interprets the question and explains the result.** Every count, ranking and duration comes from code, and every result uses the single envelope (`result_id`, `tool`, `parameters`, `data_as_of`, `generated_at`, `source_ids`, `complete`, `quality`, `truncated`, `columns`, `rows` with `evidence_refs`, `total_rows`, `limitations`, `query_version`, `trace_id`).

These semantics must be fixed before code. Otherwise the same question can return different numbers in chat, reports and dashboards, and the golden tests would have nothing to assert. Each decision below therefore ends with a golden-test case ID that becomes a fixture.

**Assumptions (not verified):**
- The decisions' `external_event_id` is the same thing as the contract's `source_event_id`.
- `source_code` identifies the source.
- Timezone examples in tests use an IANA zone chosen by Ahmed.

---

## 2. Decisions

### D-01: What counts as a door-opening event
**Decision.**
- `DOOR_OPENED` is counted when there is reliable evidence the door physically opened, from a door contact sensor or an explicit vendor event confirming physical opening.
- `DOOR_FORCED_OPEN` (catalogue type `door_forced`, class ACTIVITY) is **not** counted as a normal opening and is never added to `opening_count`. It is an alarm, and contract 1.0 cannot express that it confirms a physical opening. It is reported separately (`uncounted_door_forced_events`). *(A1: supersedes the earlier text that counted it when the source confirmed a physical opening. Counting forced openings in their own metric needs a contract field and is **NEEDS_DECISION**, see section 6 b.)*
- `ACCESS_GRANTED` is not counted: it means access was authorised, not that the door opened.
- `UNLOCK_COMMAND` is not counted by itself: it is an instruction, not proof of opening.
- `DOOR_OPENED` and `DOOR_FORCED_OPEN` are never linked, because contract 1.0 has no correlation identifier or other reliable link. When a forced event occurs in the after-hours windows the opening count may be an underestimate, so the result is `PARTIAL` with `CORRELATION_UNAVAILABLE` (never `COMPLETE`). If a reliable link is added to the contract, a linked pair may be treated as one opening; until then no link is assumed.
- Events are never treated as the same opening merely because their timestamps are close. Without a reliable link, no assumption is made.

**Rationale.** Only physical evidence answers "did the door open?".
**Consequences.** Rows keep the original event type. Migration 014 added `door_opened` and `access_granted` (authorisation only, never an opening); `unlock_command` is still not in the catalogue and is not counted. A forced event outside the after-hours windows cannot affect an after-hours count and does not downgrade the result.
**How it is verified.**
- **G-DOOR-01:** `access_granted` and `unlock_command` events with no physical-open evidence give 0 counted openings.
- **G-DOOR-02:** a `DOOR_OPENED` and a `DOOR_FORCED_OPEN` (even with a fixture-only correlation ID, which contract 1.0 cannot carry) give 1 counted opening, `uncounted_door_forced_events` = 1, status `PARTIAL`, limitation `CORRELATION_UNAVAILABLE`.
- **G-DOOR-03:** the same two events with no shared identifier and timestamps 1 second apart give 1 counted opening plus the same disclosure; close timestamps are never a reason to merge or to count the forced event.

### D-02: Is exactly 20:00 included?
**Decision.** Yes. The default after-hours window starts at 20:00:00 building-local time, inclusive, and for v0.2 ends at the next local midnight (the following day is not included). The start is configurable via `after_local`. All calculations use the building's local timezone.
**Rationale.** A fixed, inclusive boundary removes off-by-one disputes.
**Consequences.** `after_local` is echoed in `parameters`. Local-to-UTC conversion happens before querying (D-10).
**How it is verified.**
- **G-DOOR-04:** openings at 19:59:59, 20:00:00 and 20:00:01 local give counts 0, 1, 1.
- **G-DOOR-05:** an opening at 23:59:59 is counted, and one at 00:00:00 the next local day is not counted for the previous day.

### D-03: Missing timezone or ambiguous date/time input
**Decision.**
- Every building must have a valid configured timezone.
- Neither the server timezone nor UTC is ever a fallback.
- A date-only value (e.g. 2026-11-01) is interpreted in the building's local timezone.
- A full timestamp must be ISO 8601 with an explicit UTC offset.
- Local date-times without a zone or offset are rejected.
- A missing or invalid building timezone yields an explicit cannot-compute result stating that no reliable timezone is available.

**Rationale.** A guessed timezone silently shifts every boundary.
**Consequences.** Analytics must not reuse the alert engine's compatibility defaults. The envelope needs a cannot-compute outcome (Open question g).
**How it is verified.**
- **G-TZ-01:** a building with NULL timezone gives a cannot-compute result with a timezone reason and no rows.
- **G-TZ-02:** the input `2026-11-01T20:00:00` (no offset) is rejected with a validation error.

### D-04: Duplicate events
**Decision.**
- Duplicates are identified by `source_code` + `external_event_id`, and the same source event is counted once.
- If no reliable `external_event_id` exists, a data-quality issue is recorded. Events are never deleted because timestamps or payloads look alike.
- Different events of one physical opening are related only via a Correlation ID or other reliable source relationship (see D-01).

**Rationale.** Identity, not similarity, defines a duplicate.
**Consequences.** Legacy rows with NULL `source_event_id` need explicit handling (Open question f).
**Identity limits (A1, verified in migrations 006/007).**
- `source_id` + `source_event_id` is the only identity. It is enforced by the unique index `uq_event_source_event_id` when both are non-null; the tools also collapse such duplicates defensively and report them (`duplicate_events_ignored`).
- Legacy rows (`source_event_id` NULL) are protected only by `uq_event_legacy_natural_key` on (asset, event type, timestamp). That key is not an identity: two genuinely different legacy events at the same asset, type and instant **cannot both be stored**. This is an unavoidable storage limitation of the legacy path, not something the tools can repair.
- Legacy rows also have `source_id` NULL, so they form a separate device group from connector rows of the same asset. They are counted individually and flagged `MISSING_EVENT_IDENTITY`; they are never merged by similarity.
- A connector without a stable source event ID gets no deduplication guarantee. It must supply a stable ID, or accept that duplicate deliveries are counted and flagged.
**How it is verified.**
- **G-DUP-01:** the same event delivered 3 times with the same identity gives 1 counted event.
- **G-DUP-02:** two events with no event id, identical timestamp and payload give 2 counted events plus a data-quality flag (in the legacy path such a pair cannot both be stored, see identity limits).

### D-05: Device types in offline-frequency analysis (v0.2)
**Decision.**
- CCTV and Network are included when the source provides explicit, reliable Offline/Online transitions.
- HVAC and Fire Alarm are not included merely because fault or alarm events exist.
- Other types may be added later if their sources provide explicit, reliable transitions.
- A device is never assumed offline because data stopped or a fault/alarm occurred.

**Rationale.** Silence and faults are different facts from "offline".
**Consequences.** HVAC/Fire systems are absent from results (not zero). Source silence stays in the freshness/liveness model (`SOURCE_SILENT`).
**How it is verified.**
- **G-OFF-01:** an HVAC asset with `hvac_fault` events gives no offline rows.
- **G-OFF-02:** a CCTV asset whose source goes silent, with no offline event, gives 0 outages.

### D-06: Outage start and end
**Decision.**
- `OFFLINE_START` begins an outage.
- A repeated `OFFLINE_START` during an ongoing outage belongs to that outage and creates no new incident.
- `OFFLINE_END` ends an outage.
- An `OFFLINE_END` with no known start is recorded as an incomplete transition and creates no outage.
- With no `OFFLINE_END`, the outage stays open.
- The last reliable transition before the period determines the initial state. Last `OFFLINE_START` with no later end means ongoing; last `OFFLINE_END` means recovered; unavailable or unreliable means UNKNOWN (never assumed Online).

State machine (pseudocode):

state: UNKNOWN | ONLINE | OFFLINE
OFFLINE_START: ONLINE|UNKNOWN -> OFFLINE (new session); OFFLINE -> OFFLINE (no new session)
OFFLINE_END: OFFLINE -> ONLINE (close session); ONLINE|UNKNOWN -> record orphan, no session

**Rationale.** Explicit transitions are the only evidence of state.
**Consequences.** Result reports an orphan-`OFFLINE_END` count.
**How it is verified.**
- **G-OFF-03:** start, start, end in one period gives 1 session.
- **G-OFF-04:** an end with no prior start and no known state gives 0 sessions and orphan count 1.
- **G-OFF-05:** a start with no end gives 1 open session.

### D-07: Flapping
**Decision.**
- Each confirmed Offline→Online→Offline cycle is a separate outage session.
- Sessions are never merged because they are close in time.
- Each session and its evidence is preserved independently.
- FLAPPING classification is disabled until an explicit time window is configured.
- Once configured, it only classifies and never changes the session count.
- No default window (such as 5 or 10 minutes) may be introduced without an approved decision.

**Rationale.** Counting stays truthful. Classification is a later, separate layer.
**Consequences.** Result carries a FLAPPING flag that is "not evaluated" while disabled, with a limitation.
**How it is verified.**
- **G-OFF-06:** three start/end cycles within one hour, with no window configured, give 3 sessions and flapping not evaluated.
- **G-OFF-07:** the same data with a window configured still gives 3 sessions.

### D-08: Outage that began before the period
**Decision.**
- It is not counted as started within the period.
- Only the overlapping duration is counted.
- The result indicates that it was already ongoing at period start.
- Outages started in the period are reported separately from total downtime in the period.
- The last reliable earlier transition is read to determine this. If none exists, the state is UNKNOWN, and the system assumes neither an outage nor its absence.

**Rationale.** Prevents double counting across consecutive periods.
**Consequences.** Analytics must read events before `start` (Open question d).
**How it is verified.**
- **G-OFF-08:** a start at 23:00 on day 0 and an end at 01:00 on day 1, queried for day 1, gives started=0, ongoing-at-start=1, overlapped downtime=1 h.
- **G-OFF-09:** the same query with no earlier transition known gives state UNKNOWN, with no outage assumed.

### D-09: Outage with no recovery event
**Decision.**
- The session stays open and no recovery timestamp is invented.
- Duration is computed to the **earlier** of the period end and `data_as_of`.
- The result states the outage is open and the duration is a lower bound.
- The device is never claimed to be back Online without a reliable recovery event.

**Rationale.** Avoids inventing facts and avoids counting beyond known data.
**Period completeness (A1, H1).**
- A period is complete only when trusted coverage reaches the requested period end. The query execution time is never evidence of coverage.
- `state_at_period_end` (OFFLINE | ONLINE | UNKNOWN) is known only when coverage is confirmed for the whole period **and** the duration cutoff is the period end itself. If coverage or `data_as_of` ends earlier, the period is `PARTIAL` and the state at period end is `UNKNOWN`, even if the device was OFFLINE at the last observation.
- `open_at_period_end` is kept for compatibility and means only "no recovery event was observed up to the cutoff". It is not a claim about the state at the period end; read `state_at_period_end`. Renaming it is **NEEDS_DECISION**.
**Consequences.** Result needs `open_at_period_end`, `state_at_period_end` and `duration_is_lower_bound`.
**How it is verified.**
- **G-OFF-10:** an open outage with `data_as_of` before the period end has duration measured to `data_as_of`, lower bound = true.
- **G-OFF-11:** `data_as_of` after the period end has duration measured to the period end, still open, lower bound = true, `state_at_period_end` = OFFLINE (coverage confirmed).
- **G-OFF-12:** trusted coverage ends 1 h before the period end: status `PARTIAL`, `complete` = false, duration measured to the coverage end, `state_at_period_end` = UNKNOWN.

### D-10: Period boundaries
**Decision.**
- Intervals are half-open `[start, end)`.
- Selecting Nov 1–Nov 7 includes all seven days, with an internal end of local midnight at the start of Nov 8.
- Date-only values are local-zone dates, full timestamps carry an explicit offset, and offset-less local values are rejected.
- Boundaries are converted to UTC before querying, and queries use `[start_utc, end_utc)`.
- If local midnight does not exist (DST), use the first valid instant of that day.
- If it occurs twice, use the first occurrence.
- A nonexistent or ambiguous local time that these rules cannot resolve yields a validation error, never a guess.
- The server timezone is never a fallback.
- Regression tests for the gap and overlap rules use synthetic `tzinfo` objects, so they do not depend on the host's installed tz database. Tests that read a real zone (for example Havana, whose 2026-11-01 fall-back was cancelled in tzdb 2026f) are skipped, never weakened, on a host whose database predates the rule.

**Rationale.** Deterministic, repeatable conversion; adjacent periods never overlap.
**Consequences.** Normalised parameters are echoed (Open question i).
**How it is verified.**
- **G-PER-01:** an event at exactly `end` is excluded and one at exactly `start` is included.
- **G-PER-02:** the range Nov 1–Nov 7 has an internal end of Nov 8 00:00 local, converted to UTC.
- **G-PER-03:** a fixture zone with a DST-skipped local midnight uses the first valid instant of that day (expected UTC value taken from the IANA database).

### D-11: Authoritative timestamp
**Decision.**
- `observed_at` is the authoritative time for historical analytics, and `received_at` is used for ingestion delay and freshness.
- `quality` (overall event quality) is distinct from `timestamp_quality` (GOOD, UNTRUSTED, MISSING).
- GOOD events may be used in time-based analytics.
- UNTRUSTED events are excluded from analytics needing a reliable time.
- A missing `observed_at` is never replaced by `received_at`. Such events are kept in a Quarantine/Rejected path with `received_at` and the reason.
- No artificial timestamp is stored as the occurrence time.
- Storage must respect existing `observed_at` constraints and stay compatible with contract 1.0.

**Rationale.** Occurrence time and arrival time answer different questions.
**Consequences.** Contract 1.0 events have no quality field, so representation is open (Open question f).
**How it is verified.**
- **G-TS-01:** an event with missing `observed_at` is not counted and appears in quarantine with `received_at` and a reason.
- **G-TS-02:** an UNTRUSTED event is excluded and mentioned in limitations.
- **G-TS-03:** a late-delivered GOOD event is placed by `observed_at`, not `received_at`.

### D-12: Missing or incomplete data
**Decision.**
- `complete=true` only when all required data and pages were retrieved and validated.
- `complete=false` when a source fails, pages are missing or coverage is incomplete.
- `truncated=true` when results are shortened by a limit.
- `limitations` explains gaps.
- The connector must report an explicit retrieval-completion status (pages retrieved, `next_cursor` existence, confirmation all required pages were retrieved, errors). The absence of `next_cursor` is not proof of completeness unless the connector contract guarantees it.
- Partial results may be returned if clearly marked.
- The system distinguishes: unsupported by the source; supported with missing data; complete with zero results.
- Missing data is never proof that nothing happened.

**Rationale.** Silent partial answers are worse than visible ones.
**Consequences.** Requires connector reporting (Open question g).
**How it is verified.**
- **G-COMP-01:** a page failure on page 2 of 3 gives `complete=false`, with a limitation, and partial rows allowed.
- **G-COMP-02:** a source without `history.events` is reported as unsupported, not zero.
- **G-COMP-03:** a fully retrieved query with no matching events gives `complete=true`, `total_rows=0`.
- **G-COMP-04:** no `next_cursor` and no completion guarantee gives `complete=false`.

### D-13: Outage ranking metric (A1)
**Decision.** Rows are ordered by `outages_started_in_period` (sessions that began inside `[start, end)`), then building, source, external id, asset code. This was fixed as "team confirmation" in fixture revision 0.2 and is now recorded here; the ADR owner should re-confirm it at sign-off. `downtime_seconds_in_period` is a separate, explicitly selectable metric (`rank_by=downtime_seconds`) and is never substituted silently.
- Outages that started before the period and continue into it are reported in `outages_ongoing_at_period_start` and do **not** count as started, so a device down for the whole period ranks below a device with one short outage under the default metric. How they should rank is **NEEDS_DECISION**.
- Outages still open at the end are reported through `open_at_period_end` and `state_at_period_end`, not through the ranking metric.
**How it is verified.** `RankingMetricTests` in `tests/unit/test_devices_offline_frequency.py`; fixtures S-M, S-R.

---

## 3. Glossary

| Term | Meaning |
|---|---|
| `DOOR_OPENED` | Reliable evidence of physical door opening (D-01). |
| `DOOR_FORCED_OPEN` | Physical opening confirmed by the source, kept distinct as forced (D-01). |
| `ACCESS_GRANTED` | Authorisation only; not an opening. |
| `UNLOCK_COMMAND` | Unlock instruction only; not an opening. |
| `OFFLINE_START` / `OFFLINE_END` | Explicit transitions that begin and end an outage (D-06). |
| `SOURCE_SILENT` | Source liveness state (SILENT). It never means the device is offline. |
| `FLAPPING` | Classification of repeated outages; disabled until a window is configured (D-07). |
| `data_as_of` | Last reliable data timestamp for the result. |
| outage session | One confirmed Offline period, with its own evidence (D-07). |

**Relation to the Core catalogue (Implemented in Migration 014).** The glossary terms are canonical UPPERCASE concepts. The Core catalogue stores lowercase `event_types.name` values. Migration 014 adds `door_opened` and `access_granted` to the catalogue, together with `event_class`, `outage_class`, and `resolves_event_type_id` metadata for data-driven event classification and pairing. Existing event names and IDs are preserved. The legacy pairing lists remain unchanged for now and are verified against the database catalogue by automated parity tests. Connectors remain responsible for mapping vendor values to canonical catalogue names. The catalogue does not currently add `unlock_command` or generic offline/online event types.

**Canonical concept to catalogue name (A1, verified against migration 014 and `tests/fixtures/event_catalog_014.json`).**

| Canonical | Catalogue `event_types.name` | `event_class` | `outage_class` | Counted as |
|---|---|---|---|---|
| `DOOR_OPENED` | `door_opened` | ACTIVITY | none | physical opening (`doors.after_hours`) |
| `DOOR_FORCED_OPEN` | `door_forced` | ACTIVITY | none | not an opening; disclosed separately |
| `ACCESS_GRANTED` | `access_granted` | ACTIVITY | none | nothing (authorisation only) |
| `UNLOCK_COMMAND` | not in catalogue | n/a | n/a | nothing |
| `OFFLINE_START` / `OFFLINE_END` | `camera_offline`/`camera_online`, `link_down`/`link_up` | FAULT_OPEN / FAULT_CLOSE | AVAILABILITY | outage sessions |
| (not offline evidence) | `hvac_fault`/`hvac_recovered`, `fire_alarm_triggered`/`alarm_cleared` | FAULT_OPEN / FAULT_CLOSE | FAULT | never offline |

Each closer's `resolves_event_type_id` points at its opener of the same `outage_class` (`link_up` to `link_down`, `camera_online` to `camera_offline`, `alarm_cleared` to `fire_alarm_triggered`, `hvac_recovered` to `hvac_fault`); this is checked by `tests/unit/test_event_catalog_parity.py` and `tests/integration/test_event_catalogue_migration.py`.

---

## 4. Result schemas (A1: implemented in v1 of both tools; field semantics follow D-01..D-13, additions beyond them stay Proposed)

All fields sit inside the common envelope (`rows[].evidence_refs`, `limitations`, etc.).

**`devices.offline_frequency`, one row per device (Proposed)**

| Field | Purpose |
|---|---|
| device identity (`building_code`, `source_code`, `asset_external_id`) | Stable identity, also used for ordering (C) |
| `outages_started_in_period` | Sessions starting in `[start, end)` |
| `outages_ongoing_at_period_start` | Sessions started earlier and still open at `start` (D-08) |
| `downtime_seconds_in_period` | Overlap only (D-08) |
| `open_at_period_end` | No recovery observed up to the cutoff (D-09); not a claim about the period-end state |
| `state_at_period_end` | OFFLINE / ONLINE / UNKNOWN; known only when coverage reaches the period end (D-09, H1) |
| `duration_is_lower_bound` | True if any session is open (D-09) |
| `flapping` | true / false / `not_evaluated` (D-07) |
| `orphan_offline_end_count` | Ends without a known start (D-06) |
| `initial_state` | OFFLINE / ONLINE / UNKNOWN (D-06, D-08) |
| `sessions[]` | Per session: start, end (nullable), overlap seconds, `evidence_refs` |

**`doors.after_hours`, one row per door (Proposed)**

| Field | Purpose |
|---|---|
| door identity | Door/asset identity |
| `opening_count` | Counted physical openings (D-01, D-02) |
| `evidence_refs` | The events behind the count |
| `limitations` | Excluded/untrusted events, missing correlation, incompleteness |

---

## 5. Required follow-ups in the system (all Proposed)

| # | Follow-up | Type | Suggested owner |
|---|---|---|---|
| 1 | Add catalogue entries/mapping for door-open evidence (`DOOR_OPENED`) and canonical-term mapping | Schema/catalogue | Ahmed + Amr. **Done in migration 014**; vendor connector mapping still open |
| 2 | Add a data-driven open/close pairing (previously proposed `event_class` / `resolves_event_type_id`) in place of three hand-maintained code locations | Schema/catalogue | Amr. **Done in migration 014**; the legacy lists are intentionally unchanged and guarded by parity tests |
| 3 | Require a valid building timezone for analytics; no reuse of alert-engine defaults | Schema/analytics | Ahmed |
| 4 | Event correlation link field in the contract | Contract | Amr |
| 5 | Representation of `timestamp_quality` and a quarantine path compatible with contract 1.0 | Contract/schema | Joint |
| 6 | Connector retrieval-completion reporting (pages, cursor, errors) | Connector | Amr |
| 7 | Identity and flagging of legacy rows with NULL `source_event_id` | Analytics | Ahmed |
| 8 | State machine, look-back and overlap logic (D-06, D-08, D-09) | Analytics | Ahmed |
| 9 | Date/DST normalisation with the IANA database; echo normalised parameters | API | Ahmed |
| 10 | Deterministic ordering per C | Analytics | Ahmed |
| 11 | Golden fixtures G-* from section 2 | Tests | Joint |

---

## 6. Open questions

For each, the recommended default is **Proposed**, not decided.

**a. After-hours window end.** D-02 and A define the start and the next-midnight end for v0.2. Open: are hours between local midnight and opening (early morning) after-hours?
- Options: (1) 20:00 to midnight only, as decided; (2) add an optional `before_local` end for a window crossing midnight, and treat early-morning hours as after-hours when it is set.
- Recommended (Proposed): (1) for v0.2, with a limitation note in results; introduce `before_local` as a separate parameter later, as A allows.

**b. Contract expression of a confirmed forced opening.**
- Options: (1) a flag in the `door_forced` `details`; (2) a separate `DOOR_OPENED` event linked by correlation ID.
- Recommended (Proposed): (2) where the source supports it, since the link is explicit, with (1) only if the source states physical opening explicitly. No time-window matching is introduced.
- **A1:** until the contract carries such a link or flag, `DOOR_FORCED_OPEN` is not counted and no link is assumed (D-01). Whether a confirmed forced opening should be counted in its own metric is **NEEDS_DECISION**.

**c. Ranking.** Tie-break order is decided (C). Open: the default metric.
- Options: outages started, or overlapped downtime. Note C ranks by outage-session count.
- **A1:** resolved by D-13 (default `outages_started_in_period`, downtime selectable). How ongoing-at-start sessions rank remains **NEEDS_DECISION**.

**d. Look-back horizon.**
- Options: (1) unbounded: read the last reliable transition per device; (2) a bounded horizon.
- Recommended (Proposed): (1), since D-08 and D-06 ask for the last reliable transition, with no number introduced. Limitation text when none is known (Proposed): "No earlier state was available; the device state at period start is UNKNOWN, so outages in progress at that time may be missing."

**e. FLAPPING setting.** Options: disabled with an explicit limitation, or computed with a default. D-07 forbids a default, so recommended (Proposed): disabled, flag `not_evaluated` plus limitation. Storage: per building with a global fallback is an option, but needs team decision.

**f. Representation of untrusted time and missing identity.**
- Events have no quality field, and `observed_at` cannot be NULL in the database. Options: (1) add a `timestamp_quality` field to the contract; (2) keep the contract unchanged and route untrusted/missing-time records to a separate quarantine store.
- Recommended (Proposed): (2) for MISSING (unrepresentable in `events`), and (1) as an optional contract extension for UNTRUSTED.
- Legacy rows with NULL `source_event_id`: count each individually, flag them as untrusted-identity in limitations, and never merge by similarity.
- Assumption to confirm: `external_event_id` equals contract `source_event_id`.

**g. Completeness mechanism.** Today nothing records that a connector fetched every page.
- Proposed minimum: the connector reports, per source, the covered time range and a completion status; the Core stores it; analytics compares it to the requested period. Needs agreement with Amr.
- Proposed result status enum: `COMPLETE`, `PARTIAL`, `UNSUPPORTED`, `CANNOT_COMPUTE`. `UNSUPPORTED` means the connector does not declare the capability (e.g. `history.events`). `PARTIAL` means supported but data is missing or a page failed. `COMPLETE` with `total_rows=0` is a legitimate zero result. `CANNOT_COMPUTE` covers D-03 timezone and validation failures.

**h. Which pairs count as OFFLINE in v0.2.**
- Per D-05/H the candidates are `camera_offline`/`camera_online` (CCTV) and `link_down`/`link_up` (Network), the only availability-type pairs.
- Excluded: `hvac_fault`/`hvac_recovered` and `fire_alarm_triggered`/`alarm_cleared`, which are fault-type.
- Adding another type needs: a system type or event-class entry, explicit offline/online event types in the catalogue, connector mapping, and compatible pairing data (follow-ups 1 and 2). Source transitions must be explicit.

**i. Date handling in the API.**
- Options: local calendar dates with an inclusive end date; UTC timestamps; both.
- Recommended (Proposed): accept both, as D-03/D-10 already allow. Echo in `parameters` the original input, local dates, building timezone name, and resulting `start_utc`/`end_utc`.
- DST: the nonexistent/ambiguous midnight rule follows the IANA zone database and D-10. No other behaviour is invented here.

---

## 7. Readiness levels (A1)

A fixture label is never evidence of readiness. Two levels are kept apart:

| Level | Meaning | Current state |
|---|---|---|
| `PROTOTYPE_REPLAY_VERIFIED` | A scenario is replayed by unit tests against the implemented tool on synthetic data with an injected coverage provider. | Applies to most scenarios (see the fixture `status` values). |
| Real connector readiness | A vendor connector exists, maps vendor values to catalogue names, reports retrieval completion and coverage, and a production `CoverageProvider` supplies it. | **None.** Only `connectors/reference_simulator` exists, `DEFAULT_REGISTRY` is empty, and no `CoverageProvider` is wired, so production results are `PARTIAL` / `COMPLETION_NOT_CONFIRMED`. Depends on Amr (follow-up 6). |

## 8. Out of scope

- Personnel or attendance analytics (blocked until roles and privacy authorisation exist).
- Person identifiers in any event.
- Vendor write or control actions.
- UI.