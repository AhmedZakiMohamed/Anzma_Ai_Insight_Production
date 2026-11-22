# Phase 1 implementation status

**Branch:** `main`
**Baseline commit:** `76f0169` (`test: keep freshness fixtures after migration defaults`)
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

**Status: DESIGN DRAFT — HUMAN APPROVAL PENDING.** `docs/ASSET_STATE_DESIGN.md` records the verified current behavior, additive schema and derivation proposal, compatibility boundary, test outline, and open decisions. No code, migration, API shape, or test was changed/run. Reported state remains evidence and cannot override Core health. Implementation is blocked on the human decisions listed in the design note.

### G4 — multi-building proof

**Status: NOT STARTED.** Prove two buildings and sources through ingestion with no application-code changes; verify cross-building isolation; remove only demonstrated catalogue/threshold blockers; safely require administrator-supplied `building_code`. No seed/injection retirement is authorized here.

### G5 — HistorianRepository and retention

**Status: NOT STARTED.** Implement latest/range/aggregate/gap/freshness repository reads and additive retention policy/job with keep-all default and dry-run. No historical deletion is authorized without the reviewed retention behavior.

### G6 — ingestion and query performance evidence

**Status: NOT STARTED.** Measure 5000-record ingestion, database growth, dashboard/KPI/alert/chat-fact latency, and a disposable ≥1,000,000-row telemetry dataset. Add indexes only when measured plans justify them. Record machine details in `docs/PERFORMANCE_REPORT.md`.

### G7 — simulator real-Core acceptance

**Status: PARTIAL; NOT ACCEPTED.** Run the currently skipped test against a disposable Core: down/buffer/drain, zero duplicate rows, mid-drain restart, keepalive and connector status. Prove seed/injection parity. Propose retirement separately and stop for human approval; do not retire paths.

### G8 — AI reliability and provider interface

**Status: PARTIAL; Amr overlap recorded.** Amr's `60579a0`/`bdd8d3d` work adds the `ModelProvider` interface, Ollama adapter, and grounding/validation foundation. This baseline confirms the provider unit tests and existing deterministic explanation paths. In the isolated Task A stack, the requested S1 `--llm` observation used `OLLAMA_HOST=http://ollama:11434`, model `qwen2.5:7b`, timeout 120 seconds; the isolated Ollama `/api/tags` returned 200 with an empty model list and its `/api/chat` response was HTTP 404 `model 'qwen2.5:7b' not found`. S1 returned `ai_source=fallback`, `model_used=None`, and the documented unavailable-model limitation. This confirms missing model in the isolated Ollama volume as the cause for this run; it does not establish a failure of Amr's provider interface. Provider selection and G3 evidence/sufficiency integration remain.

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
- **2026-11-22 — Task A baseline on `main`:** code/test baseline `76f0169`, isolated Compose project `anzma-taska-current-20261122` only. Unit suite 75 tests (1 skipped), data integrity 23/23, frontend/API 42/42, day2 intelligence 43/43, default S1 E2E 14/14, chat consistency 31/31, model-provider 3/3, combined freshness schema/admin/job/gaps/liveness/API 36/36. S1 `--llm` observation passed its scenario checks 15/15 but used deterministic fallback because the isolated Ollama volume had no models (see G8). No shared stack/DB was used. Current `main` includes local commits `18ac4d0`, `dfc8ddf`, `0476405`, `e3a2d3a`, and `76f0169`; no push or tag was made.
