# Anzma AI Insight G0 Repository Baseline

**Snapshot date:** 26 November 2026  
**Branch:** `main`  
**HEAD:** `58511df37d1836ccb804f92ad012f8401495c02b` — `docs: prepare installable demo release`  
**Status:** Baseline recorded; G0 release gate is **not passed**.

This snapshot prevents the CEO architecture work from being mistaken for a clean or release-ready code baseline. It records the exact repository state observed during planning. It is not a test report and does not authorize discarding, resetting, or recommitting any working-tree changes.

## 1. Repository state observed

### Execution check on 26 November 2026

The current inspection confirms branch `main`, HEAD `58511df37d1836ccb804f92ad012f8401495c02b`, one commit behind `origin/main`, no staged changes, and 34 modified/untracked status entries. The worktree remains dirty. This is a read-only observation; no fetch, merge, reset, cleanup, commit, push, or database migration was performed.

The earlier G1 inventory below is not the whole current worktree. Additional uncommitted Amr-scope work now includes the common connector SDK, typed AI tool routing, alert replay/backfill, dashboard reconnect handling, and focused tests. Uncommitted architecture/product documents and presentation deliverables are also present. See `git status --short` for the exact paths at the time of review. Preserve all of these until their owners have reviewed and accepted or explicitly rejected them.

Node.js is available. Docker Desktop became available after the user opened it. Python is not installed on the host, so verification used a uniquely tagged Docker dependency image and a disposable Compose project. The existing `anzma-core` project was not used for tests or migrations.

### Current candidate verification (26 November 2026)

The candidate is the recorded `main` HEAD plus the uncommitted working-tree changes listed below. It is not a commit or release tag. Results:

| Check | Result | Scope / qualification |
|---|---|---|
| Dashboard inline JavaScript and chat-widget JavaScript syntax | **Passed** | Parsed with Node.js. |
| `git diff --check` | **Passed** | Checked tracked-file diffs; untracked files are not included by Git's command. |
| Focused in-scope unit suite | **37/37 passed** | Connector SDK, reference REST, analytics/chat tools, alert replay API and dashboard alert center. |
| Full `tests/unit` suite | **120 passed, 1 skipped** | Ran from a temporary copy of the current worktree in an isolated Python 3.11 container. Shell scripts were normalized from CRLF to LF only in that temporary copy because the Windows checkout's CRLF shebangs otherwise fail under Linux. The skipped check explicitly requires a separately configured staging Core. |
| Fresh database schema | **21/21 checks passed** | New project-scoped PostgreSQL volume; migrations 013 and 014 applied; all four G1 event types present. |
| Migration 013 integration | **2/2 passed** | Test-created databases were created and dropped inside the disposable PostgreSQL cluster. |
| Reference connector → Core ingest | **8 accepted, 0 rejected** | Synthetic HTTP source, source-scoped credential, and four source/asset mappings in the disposable database. |
| Duplicate replay | **8 duplicates, 0 new, 0 rejected** | Re-delivered the same stable upstream event IDs; Core committed the replay without duplicate records. |
| G1 connected chat/API acceptance | **3/3 passed** | Required auth, typed/chat result parity and golden fixture evidence IDs. |
| API readiness | **Passed** | Disposable API reported database ready. |
| Browser smoke and building filter | **Passed at narrow in-app viewport** | Dashboard loaded with live stream status; selecting Data Center Annex reduced visible alerts 10→4 and incidents 3→2. At the approximately 304px viewport, cards and controls overflow horizontally. This is not a desktop-width layout qualification. |
| Real Socket.IO interruption/reconnect in browser | **Not run** | Replay behavior is covered by unit assertions; a live browser disconnect/reconnect drill remains outstanding. |

The first unnormalized Linux unit run failed six shell-script tests because CRLF shebangs were read as `bash\r`; the rerun in a temporary normalized copy passed. The project files were not line-ending modified. During initial G1 setup, an operator token was mistakenly used where the source key was required; the request correctly failed with 401, no records were ingested by that attempt, and the corrected source-key run passed. Temporary source keys/tokens and database contents existed only in the disposable project.

**G0 disposition:** verification checks are complete for the current candidate, but G0 remains **not passed**. The worktree is still mutable/uncommitted; the data owner has not reviewed the canonical event/analytics semantics; the deployment and pilot-source decisions are still open; and independent desktop/reconnect review remains outstanding. Do not treat the synthetic source result as real-vendor qualification.

**Follow-on G1 change (26 November 2026):** after the verification run above, the reference connector gained an explicit page-completeness check and a local SQLite outbox/checkpoint with replay-after-lost-response behavior. Its focused module now passes **19/19**. The earlier full-unit count of 120 passed / 1 skipped applies to the earlier candidate and has not been rerun after this G1 change. This additive work does not change the G0 gate status or qualify a real vendor. See [`v02/G1_FIRST_SLICE.md`](v02/G1_FIRST_SLICE.md) and [`../connectors/reference_rest/README.md`](../connectors/reference_rest/README.md).

### Modified tracked files

| Path | Current change category |
|---|---|
| `app/api/chat_lite.py` | Early analytics/chat integration work. |
| `app/dashboard/static/chat-widget.css` | G1 result table presentation. |
| `app/dashboard/static/chat-widget.js` | G1 structured result rendering. |
| `app/main.py` | Analytics API/router integration. |
| `docs/API_CONTRACT.md` | Proposed/current API additions for the G1 slice. |
| `docs/CHANGE_COORDINATION.md` | Coordination notes for current implementation work. |
| `README.md`, `docs/README.md` | Product architecture navigation added during this project setup. |

### Untracked implementation and verification work

- `app/api/analytics_api.py` and `app/services/analytics/`
- `connectors/reference_rest/` including its fixture, manifest, connector and service container definition
- `db/migrations/014_operational_analysis_events.sql`
- `tests/integration/test_g1_connected_chat.py` and `tests/unit/test_reference_rest_g1.py`
- `docs/v02/G1_FIRST_SLICE.md`

The implementation and its tests are not committed at the recorded HEAD. Preserve them during review. The G1 record says migration 014 must be applied through the migration runner; this baseline did not verify migration 014 in a current target environment.

### Uncommitted project-architecture material

`docs/PROJECT_CHARTER.md`, `docs/WHOLE_SOLUTION_ARCHITECTURE.md`, `docs/ARCHITECTURE_DECISION_REGISTER.md`, `docs/ANALYTICS_CAPABILITY_CATALOG.md`, `docs/PRODUCT_REQUIREMENTS_TRACEABILITY.md`, `docs/PILOT_SYSTEM_QUALIFICATION_TEMPLATE.md`, `docs/CEO_ARCHITECTURE_REVIEW_BRIEF.md`, `docs/DELIVERY_ROADMAP.md`, and `docs/product_reference/` were added during the CEO-direction and architecture work. The seven preserved DOCX/JPEG copies were hash-checked against their supplied originals.

### Updates recorded on 26 November 2026

- `WHOLE_SOLUTION_ARCHITECTURE.md` now contains the user's working workload profile, the conditional 77.76 million-row calculation, RAM planning arithmetic, S0–S3 sizing, and CPU/GPU comparison. Values remain assumptions; the 248-asset target, five user roles, and production connector are not established by the current prototype.
- `PILOT_SYSTEM_QUALIFICATION_TEMPLATE.md` now asks for point-count scope, enforced user roles, AI concurrency, GPU/host sizing, and corresponding workload and AI acceptance evidence.
- `ARCHITECTURE_DECISION_REGISTER.md` records ADR-12 for optional RAG/GraphRAG retrieval and ADR-13 for platform metrics monitoring such as Graphite. Both remain open and neither is in the current deployment.
- `CEO_ARCHITECTURE_REVIEW_BRIEF.md` now includes the workload assumptions and deployment sizing proposal for review; it explicitly keeps sizing and AI latency unapproved.
- `deliverables/Anzma_AI_Insight_Deployment_Scenarios_CEO_Aligned_Sizing_Review_v2.pptx` is the latest 12-slide decision deck. Its validation receipt reports package pass, 0 layout findings, 0 layout warnings, 11 native tables, and successful first-party import. The validator does not verify native PowerPoint rendering.
- Prior deck versions remain in `deliverables/`; the v2 file is the corrected version after resolving the scale-path table overflow.

## 2. G1 implementation status evidence

[`v02/G1_FIRST_SLICE.md`](v02/G1_FIRST_SLICE.md) reports an initial synthetic reference REST → connector → Core → typed analytics/chat proof. It records test and disposable-environment results dated 24 November 2026, then explicitly states:

- The slice is not an accepted milestone.
- It uses synthetic data and preconfigured external-ID mappings; it is not a real vendor connection or customer-run setup wizard.
- The result snapshot and source synchronization/checkpoint contract are absent; source coverage remains unknown.
- Natural-language handling is limited to two explicit ISO dates.
- Browser visual behavior was not verified.
- Analytics/event semantics require owner review.

These are project records for their named run. No tests or runtime checks were run for this baseline snapshot, so they do not prove the present working tree still passes.

## 3. G0 disposition

G0 verification is **complete for the current candidate; the gate is not accepted**. The current candidate is the recorded `main` HEAD plus the uncommitted worktree. The test and disposable-runtime results above provide current evidence, but the worktree is not an immutable revision. Acceptance still needs owner review of canonical event and analytics semantics, deployment and pilot-source decisions, and independent desktop/reconnect review. The synthetic connector does not qualify any real vendor.

### Initial Amr-scope code review (26 November)

This is a static review of the current worktree, not an acceptance test. Keep the findings visible for the next bounded work package:

| Area | Finding | Disposition / owner |
|---|---|---|
| Connector SDK and reference REST | The shared contracts distinguish declared capabilities from empty results and page continuation from source completeness. The synthetic reference connector supports only event history and now has a local SQLite outbox/checkpoint; Core does not retain range-level sync coverage, and the connector has no customer-managed lifecycle. | Keep as a contract/example under Amr's integration scope; review vocabulary and event semantics with the data owner. Real vendor support remains unproven. |
| Analytics API and chat | The two typed analytics execute deterministically, but coverage is `UNKNOWN`; their result IDs are not persisted snapshots. The route requires explicit ISO dates and handles only two intents. | Treat as a technical precursor, not the general analytics catalog or broad natural-language experience. Coordinate formulas/semantics with their owner. |
| AI routing | The two new analytics use bounded typed tool calls, but other questions can fall through to the pre-existing SQL agent. | Do not claim the chat is fully registry-driven or free of model-authored SQL. Expanding the typed catalog and retiring/restricting the legacy path needs a separate planned change and regression review. |
| Chat language and status | Successful answers in the new analytics route are currently composed in English even when the request is Arabic. The response also marks `data_sufficient=false` while saying upstream coverage is unknown. | Add Arabic answer parity and clarify the UI meaning of the conservative sufficiency flag within the AI/chat work package. |
| Alert replay | Replay returns newly persisted alert rows and current status; it is not a durable event log for acknowledge/resolve transitions. The dashboard cursor fallback was changed in the working tree on 26 Nov: it now waits for the global watermark instead of advancing from a filtered visible alert list. | Static fix recorded; browser/reconnect verification remains pending. Durable status-transition history requires a separately approved data-model change. |
| User authorization | Current REST routes are protected by one shared operator token/session; no per-user role or building scope is enforced. | Treat role-based views as prototype-only until identity and server-side authorization are approved and implemented. Do not rely on UI filtering as security. |
| Verification | Current-worktree unit, migration, ingest/replay, API and narrow browser checks were run on 26 Nov; detailed counts and limits are in section 1. | Evidence applies to the named mutable worktree candidate, not a commit/tag or release. Desktop-width visual and live browser reconnect checks remain pending. |

## 4. Remaining G0 acceptance actions

1. Keep the current worktree intact; do not `reset`, `clean`, or overwrite uncommitted files.
2. Review the G1 code and migration against the existing ingestion contract and canonical data semantics with the assigned data owner.
3. Decide whether to accept the technical G1 slice as a separate review package; do not call it CEO G1 Guided Connection.
4. Obtain the launch deployment and pilot-source decisions; keep the decision register open until approved.
5. Repeat visual/reconnect QA at a normal desktop browser width and record the shared dashboard review. Do not treat the 304px side-panel screenshot as desktop acceptance.
6. Once owners accept the candidate, create a named immutable revision and retain its verification outputs before treating G0 as passed.
6. Recalculate `git status`, commit/review state and release evidence before declaring G0 or G1 complete.

## 5. What this baseline does not establish

- It does not establish clean-clone or customer-production installation on this current code candidate.
- It does not establish real vendor compatibility, guided self-service, discovery/mapping UX, saved dashboards, reports, customer RBAC, SaaS tenant isolation, alert replay, or measured hardware specifications.
- It does not update historical test reports or imply that the current workspace has just been tested.
