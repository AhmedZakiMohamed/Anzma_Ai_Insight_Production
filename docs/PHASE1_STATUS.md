# Phase 1 implementation status

**Branch:** `main`
**Verification baseline commit:** `4b2d135` (`test: verify shadow state evidence grounding`; full regression executed against this committed code/test state on 2026-11-22)
**Status date:** `2026-11-22`
**Rule:** one numbered package per session; no push/tag; never use the shared database or running shared stacks.

This is the execution tracker, not a claim that Phase 1 is production-ready. “DONE” means the package has code, its requested disposable-environment evidence, and a committed result. The human retains all approval decisions listed below.

## Accepted work already present

- **A, B1–B3 (1.1 foundation): DONE.** Operator bearer/session authentication is behind a replaceable authenticator; ingestion source keys are separate. Per-source ingestion rate limiting, CORS allowlisting, key lifecycle audit logging, authentication options, and TLS reverse-proxy guidance are present. The final enterprise identity model remains a human decision. PostgreSQL is still published on loopback by default, so G10's “no published DB port” requirement is not complete.
- **C1–C2 (1.8 backup foundation): DONE for the scoped local rehearsal.** `scripts/backup_db.sh` creates retained backups; `scripts/restore_verify.sh` and `docs/RESTORE_RUNBOOK.md` record one successful scratch restore. This is not recurring production scheduling or a production restore.
- **D1–D3 implementation (1.2 foundation): PARTIAL.** The simulator design and separate HTTP-only package exist, including deterministic records, persisted batch IDs, durable outbox, retry/drain and keepalive behavior. The real-Core acceptance test is skipped unless explicitly configured; outage/restart and legacy-seed parity evidence still need G7. Do not retire seed/injection paths without human approval.
- **E1–E6 (1.3 foundation): IMPLEMENTED.** Freshness expectations/admin, evaluator, data-gap lifecycle, source liveness/classification, authenticated read APIs, dashboard evidence panel, and G1 building-local alert schedules are present. G2 adds an opt-in Compose evaluator service; it must be explicitly enabled for snapshots to remain current.

## Ordered work packages

### G1 — E6: building timezone and work-hours behavior

**Status: DONE — commit `d39c582`.** Alert work-hour classification and anomaly regimes now use the asset building's IANA timezone, local start/end, and non-working weekdays from migration 009. The compatibility defaults remain for NULL fields. No migration was added. `tests/unit/test_alert_schedule.py` covers the seeded UTC+2 parity window, Africa/Cairo vs America/New_York, a New York winter/summer DST offset change, and a non-working weekday.

**Evidence:** before/after on the isolated E3 PostgreSQL stack: alert run remained 10 detected / 10 new / 8 incidents created / 2 attached. Canonical JSON hashes were identical for `/dashboard/overview` (`3583318d…`), `/dashboard/trends` (`081a5fc0…`), `/dashboard/equipment-health` (`8f51672c…`), `/alerts` (`0e5fd75b…`), and `/incidents` (`411b59ec…`). The `tests/integration/test_dashboard.py` output matched byte-for-byte; KPI values included 12 monitored assets, 10 alerts, and 4 critical alerts. Baseline was captured from commit `263f09d` in a temporary detached worktree, removed after comparison.

**Verification:** `python3 -m unittest discover -s tests/unit` — 57 run, 1 skipped, pass; `python3 tests/integration/test_day2_integration.py` — 43/43 pass before and after; `python3 tests/integration/test_frontend_api.py` — 42/42 pass on both baseline and G1 code; `PYTHONPATH=. python3 tests/integration/test_dashboard.py` — pass, output identical; `git diff --check` — pass. All database-backed checks targeted disposable container `anzma-e3-freshness-20261120-db-1` through port 25443.

### G2 — scheduled freshness job

**Status: DONE — code commit `46b1099`; integration/docs commit `8517066`.** The evaluator is an opt-in `freshness` Compose profile, independent of the API/Ollama services. It uses a PostgreSQL session advisory lock for single-instance ownership, configurable interval/lock-retry settings, graceful SIGTERM/SIGINT handling, structured pass/failure lines, and Compose restart handling. Existing E3/E4 logic provides idempotent series/gap writes, source liveness and cause classification. No migration was added.

**Verification:** `python3 -m unittest tests.unit.test_freshness_job` — 9/9; `python3 -m unittest discover -s tests/unit` — 63 run, 1 skipped, pass; `tests/integration/test_freshness_job.py` — 7/7 on disposable `anzma_e3_g2_final`; `tests/integration/test_freshness_gaps.py` — 5/5; `tests/integration/test_source_liveness.py` — 6/6. Compose config confirmed profile `freshness`, no published ports, DB-only dependency, and restart policy `unless-stopped`. A real one-shot Compose run on disposable `anzma_e3_g2_baseline2` evaluated 45 series, 2 sources and acquired/released the DB lock. The live profile service ran a pass and stopped on SIGTERM with exit code 0 and `FRESHNESS_LOCK_RELEASED` in logs.

**Test adjustment:** the integration test asserts the exact output lifecycle (`FRESHNESS_LOCK_ACQUIRED`, one `FRESHNESS_PASS`, `FRESHNESS_LOCK_RELEASED`) and retains all pass-summary/count assertions. The first integration attempt used a disposable template missing migration 011; after verifying that schema mismatch, reran against a clean 011 template. The initial single-line JSON assertion failed after lock records were added; the lifecycle assertion passed 7/7.

### G3 — Core-derived asset state

**Status: DONE — shadow-only implementation; existing readers remain authoritative.** The project owner approved the decisions on 2026-11-22. Migration `012_asset_state_shadow.sql` adds `asset_state_current` and append-only `asset_state_history`, records the ledger entry, and is covered by `scripts/verify_schema.sql`. The freshness job invokes the shadow evaluator after the freshness commit while its single-instance lock is held; the state stage uses its own transaction/lock, logs failure, and preserves the prior state snapshot without rolling back freshness. First evaluation creates a real-time `BASELINE`; subsequent history is limited to semantic tuple transitions.

The projection maps existing dashboard buckets to operational state, derives Core availability only from Core events, and derives communication from series freshness only for unambiguous single-source assets. Multi-source or mixed-attribution assets are `UNKNOWN`; source liveness remains evidence. It never derives equipment `OFFLINE`/`UNAVAILABLE` from silence. Private alert evidence carries freshness, liveness, provenance and quality without changing public response shapes. `UNCERTAIN` lowers sufficiency only when a final-text citation references that measurement; grounding status is unchanged.

**Evidence on isolated project `anzma-taska-current-20261122` (PostgreSQL 16; no shared stack/DB):** full unit 83 (1 skipped); model provider 3/3; data integrity 23/23; frontend/API 42/42 including all 16 contract vectors and dashboard/API checks; Day2 43/43; chat consistency 31/31; E2 S1 with `--build --llm` 15/15 (deterministic fallback, not live inference); E2 schema/admin 12/12; E3 job 7/7; E4a gaps 5/5 twice; E4b source liveness 6/6 twice; E5 API 6/6; G3 integration 6/6 twice; G3 grounding unit 3/3. `verify_schema.sql` returned `OVERALL OK`; two runner executions skipped recorded migrations 006–012. The seeded state parity run covered 12 assets with 0 mismatches; the seven required synthetic scenarios also had 0 mismatches. Canonical sorted-JSON hashes matched between source `1737e06` and the G3 code for overview (`81ba2aa6…`), alerts (`62c28825…`), incidents (`f322be7f…`), and the same deterministic chat request (`a710019b…`).

**Commits:** schema `f332332`; evaluator `ba35301`; persistence tests `1737e06`; internal evidence/parity `22ee886`; evidence tests `4b2d135`; this documentation update is separate. No migration was applied to a shared database. The exact existing-database runner command and full hashes are in [`ASSET_STATE_DESIGN.md`](ASSET_STATE_DESIGN.md).

### G4 — multi-building proof

**Status: NOT STARTED.** Prove two buildings and sources through ingestion with no application-code changes; verify cross-building isolation; remove only demonstrated catalogue/threshold blockers; safely require administrator-supplied `building_code`. No seed/injection retirement is authorized here.

### G5 — HistorianRepository and retention

**Status: NOT STARTED.** Implement latest/range/aggregate/gap/freshness repository reads and additive retention policy/job with keep-all default and dry-run. No historical deletion is authorized without the reviewed retention behavior.

### G6 — ingestion and query performance evidence

**Status: NOT STARTED.** Measure 5000-record ingestion, database growth, dashboard/KPI/alert/chat-fact latency, and a disposable ≥1,000,000-row telemetry dataset. Add indexes only when measured plans justify them. Record machine details in `docs/PERFORMANCE_REPORT.md`.

### G7 — simulator real-Core acceptance

**Status: PARTIAL; NOT ACCEPTED.** Run the currently skipped test against a disposable Core: down/buffer/drain, zero duplicate rows, mid-drain restart, keepalive and connector status. Prove seed/injection parity. Propose retirement separately and stop for human approval; do not retire paths.

### G8 — AI reliability and provider interface

**Status: PARTIAL; Amr overlap recorded.** Amr's `60579a0`/`bdd8d3d` work adds the `ModelProvider` interface, Ollama adapter, and grounding/validation foundation. The provider unit suite passed 3/3. In the isolated Task A/G3 stack, `OLLAMA_HOST=http://ollama:11434`, model `qwen2.5:3b`, and timeout 120 seconds were observed. `/api/tags` returned HTTP 200 with `models: []`; direct `POST /api/chat` returned HTTP 404 `model 'qwen2.5:3b' not found`. E2 S1 with `--llm` passed scenario checks 15/15 through fallback with `model_used=None`. Missing model in this disposable Ollama volume is the observed cause; live inference remains **NOT TESTED**. G3 registers its internal evidence additions in grounding/validation and tests both supported evidence and continued rejection of unsupported claims. This does not establish a failure of Amr's provider interface.

### G9 — deployment and operations

**Status: PARTIAL.** Migration ledger/lock runner, `/health`, DB `/ready`, backup creation and one scratch restore exist. Deployment-time migration application, schema-version/ingestion readiness, metrics, pinned Ollama image, resource limits, log rotation, tested upgrade/rollback, and scheduled backup operation remain.

### G10 — 1.1 security remainder

**Status: PARTIAL; Amr overlap recorded.** Amr's security/Compose work includes the `ANZMA_AUTH_DISABLED` flag and deployment environment changes. Keep this flag, existing healthchecks, and documented token/provider env names stable. Existing auth/CORS/rate/audit/TLS work is listed above. Remove the default PostgreSQL host-port publication; verify repository secrets and `.env.example`; verify body limits and TLS guidance; add the security checklist. Update `AUTH_OPTIONS.md` with the migration recommendation and stop for the human's enterprise identity decision; do not choose it.

### G11 — documentation consolidation

**Status: PARTIAL.** Current docs index, E5 API/design docs, changelog and known issues exist. Mark older drafts historical without altering their recorded results; provide one authoritative index, operator-runbook links and committed OpenAPI export; check contract drift.

### G12 — final Phase 1 acceptance

**Status: NOT STARTED.** After G1–G11, run fresh-stack regression and docs-only clean install; repeat restore, multi-building and performance evidence; list open P0/P1 issues and limitations in `docs/PHASE1_ACCEPTANCE_REPORT.md`; propose a tag without creating it. Do not claim production readiness.

## Human approval gates

- Choose the enterprise operator identity/authorization model after reviewing `docs/AUTH_OPTIONS.md`.
- Approve any proposed retirement of legacy seed/injection paths after G7 parity evidence.
- Approve the G3 design note before any schema/interface implementation.

## Session log

- **2026-11-20 — G1 completed:** code commit `d39c582`, docs commit `ef8cde0`.
- **2026-11-20 — G2 completed:** code/config commit `46b1099`, integration/docs commit `8517066`. G2 database work used only the disposable E3 PostgreSQL container and isolated `anzma_e*_g2_*` databases. During this work, the shared worktree moved to `main` with many staged changes; those were left untouched, and status tracking was finished in an isolated worktree on the feature branch.
- **2026-11-21 — G3 design drafted:** `docs/ASSET_STATE_DESIGN.md`; docs-only proposal, awaiting human approval. No tests run because this is the required design gate.
- **2026-11-22 — Task A and G3 verification on `main`:** isolated Compose project `anzma-taska-current-20261122`; committed code/test state `4b2d135`. Current rerun: unit 83 (1 skipped), data integrity 23/23, frontend/API 42/42, Day2 43/43, E2 S1 `--build --llm` 15/15 (fallback; live model **NOT TESTED**), chat consistency 31/31, model-provider 3/3, E2–E5 freshness suites 36/36, G3 integration 6/6 twice, and G3 grounding unit 3/3. Schema verification was `OVERALL OK`; response hashes and parity evidence are recorded above and in `ASSET_STATE_DESIGN.md`. The API container initially restarted because its temporary staging `ANZMA_API_TOKEN` was empty; only that isolated API container was restarted with a temporary token. The isolated PostgreSQL volume and shared stacks were not recreated or changed. No push or tag was made.
