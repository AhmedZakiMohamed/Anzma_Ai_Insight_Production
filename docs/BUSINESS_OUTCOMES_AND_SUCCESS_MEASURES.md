# Anzma AI Insight Business Outcomes and Success Measures

**Status:** Measurement design proposal; numeric targets and ROI claims require pilot baselines and customer approval.  
**Companion:** [`WHOLE_SOLUTION_ARCHITECTURE.md`](WHOLE_SOLUTION_ARCHITECTURE.md)

The CEO's promises—easy integration, reliable operational answers, useful visibility and a modular product—need measurable proof. This document defines candidate measurements without assigning unsupported performance or savings targets.

## 1. Measurement principles

1. Measure a customer outcome and its quality together. A fast answer that is wrong or unsupported is not success.
2. Separate **product capability** (the configured source supports a question), **query coverage** (the requested data range is complete enough), **data freshness/quality**, and **business outcome** (an operator or operation changed).
3. Capture a baseline before deployment and compare the same site, scope, time period and operating conditions afterward.
4. Report results by source, capability, site and data category. Do not average a successful integration with unavailable data sources and claim universal support.
5. Identify customer-caused delay, vendor/network dependency, and Anzma effort separately; otherwise onboarding time and cost are misleading.
6. Treat energy savings, avoided failures, labor savings and security improvement as hypotheses until the customer approves an attribution method and evidence supports it.
7. Minimize telemetry of user behavior. Collect only the events needed to evaluate agreed adoption and workflow outcomes, with defined access and retention.

## 2. Candidate north-star measure

**Candidate:** *Share of in-scope operational questions answered with an approved calculation, authorized data, traceable evidence and an acceptable coverage state.*

This is a quality-and-coverage measure, not an agreement to a single company KPI. It would be calculated as eligible, completed requests meeting all correctness, permission, evidence and coverage criteria divided by eligible in-scope requests in the pilot. Exclude unsupported questions, but report their rate separately so the denominator cannot hide gaps. The business owner should approve the question set and minimum acceptable coverage before use.

Pair it with **time to first trusted insight** (from the start of approved setup to the first reconciled operational answer). Neither measure by itself proves customer ROI; usage and operational impact must be measured separately.

## 3. Pilot scorecard

| Outcome area | Candidate measure and definition | Evidence source | Owner | Guardrail / interpretation |
|---|---|---|---|---|
| **Self-service onboarding** | Elapsed active setup time from authorized setup start to first valid, mapped, freshness-evaluated data; report customer wait time separately. Track completion without source-code/SQL edits and number of assisted steps. | Wizard event timestamps, connector test/sync audit, support/integration time log. | Product + deployment lead | Start/stop events must be defined. A source that needs custom integration is not included in certified self-service timing. |
| **Integration effort** | Customer and Anzma hours per source/site by activity: network/security, credentials, discovery, mapping, testing, remediation. | Work log with activity tags and scope. | Project lead | Report distribution across sites; do not use one easy reference source as average. |
| **Capability fit** | Fraction of requested capabilities classified available, partial, or unavailable after discovery; time and reason to close a gap. | Connector manifest, discovery inventory, capability catalog and decision log. | Connector/product owner | Missing permissions, license and source-retention limits are distinct reasons. |
| **Mapping quality** | Correct approved mappings divided by mappings reviewed; correction/reversal rate after first approval; unresolved high-impact mappings. | Mapping review and version history; seeded wrong/ambiguous fixtures. | Data/analytics owner | Human approval is not ground truth unless reconciled to source semantics. Report by point/event type and confidence. |
| **Data reliability** | Valid/accepted record rate; duplicate/conflict/rejection rates; observed freshness versus each configured expectation; known dropped/missing-page intervals. | Ingestion batch audit, source sync/checkpoint, quality and freshness records. | Connector + data platform | Duplicates may be correct idempotent retries; don't treat them all as data loss. Unknown coverage is not complete. |
| **Analytic correctness** | Exact/approved-tolerance agreement with independently reconciled source fixtures and business-approved formula, per analytic and boundary case. | Versioned analytic contract, expected outputs, raw source references, reviewer result. | Data/analytics owner + independent QA | A general quality claim needs a representative test set; two successful examples are not broad accuracy. |
| **Answer trust** | Eligible answer pass rate: correct values, correct scope/time, valid evidence references, disclosed limitations, and proper abstention on missing/unauthorized data. | Result envelope, anonymized evaluation cases, review rubric and logs. | AI reliability + data owner | Measure model and deterministic fallback separately. Conversation count or user thumbs-up alone is insufficient. |
| **Operator workflow** | Time from actionable alert receipt to acknowledgement and resolution; evidence drill-down use; task/work-order link where available. | Alert event IDs, user audit events, CMMS/work-order timestamps. | Operations owner | Separate delivery delay from human response. Increased alert volume is not automatically value. |
| **Product adoption** | Active authorized users by role/site, repeated use of approved capabilities, successful saved-dashboard/report use. | Privacy-minimized product analytics with user scope. | Product owner | Define active use and retention period; do not collect question text if not needed. |
| **Operational impact** | Customer-selected outcome such as downtime duration, energy per operating unit, recurrence or response effort before/after. | Customer system of record (CMMS, meters, BMS) and agreed baseline. | Customer business owner | Control for operating hours, weather, occupancy, production, maintenance and unrelated interventions; distinguish correlation from attribution. |
| **Platform reliability** | API availability and latency, connector sync success/recovery, alert replay reconciliation, backup success and restore time. | Health/metrics, sync checkpoints, event cursor audit, backup/restore evidence. | Platform/operations owner | Set service objectives only after deployment profile, pilot scale and support ownership are chosen. |

## 4. Baseline and attribution plan

Before the pilot, select no more than a small set of customer-priority workflows, then:

1. Record the current process: data sources used, manual steps, time to answer, current response/maintenance outcomes, and known data gaps.
2. Define the unit of analysis (question, alert, asset, building, or operating hour), baseline period, timezone, exclusions and comparison period.
3. Confirm the source of truth for each outcome metric. For example, use CMMS records for completed maintenance work rather than treating an Anzma recommendation as proof that maintenance occurred.
4. Agree how operating context changes are handled (weather, occupancy, holidays, schedules, production/load, seasonal shifts).
5. Define whether the result is descriptive, comparative, or causal. Do not claim Anzma caused savings from a simple before/after correlation.
6. Approve numeric thresholds, acceptable uncertainty and reporting audience with the customer/business owner.
7. Retain sanitized examples and evaluator versions so results can be reproduced across releases.

## 5. Instrumentation boundaries

Candidate events include setup started/completed, connection test, capability discovery result, mapping reviewed/changed, sync started/completed/failed, analytic requested/result status, evidence opened, dashboard/report saved/exported, alert received/acknowledged/resolved, and backup/restore outcomes.

Prefer IDs, categorical statuses and timings over raw question text or source payloads. Do not log credentials, person-level access details, video, or full customer records as product telemetry. Put any diagnostic content needed for support behind explicit customer authorization and documented retention. Ensure the analytics that measures use is itself scoped to tenant/building and user permissions.

## 6. Release evidence

Each pilot review package should include:

- approved use cases and KPI definitions;
- baseline method, period, scope and limitations;
- connector/source/version and supported capability list;
- setup effort split between customer and Anzma;
- mapping review quality and unresolved conflicts;
- freshness, completeness, quality and recovery results;
- analytic correctness and answer-trust evaluation by operation;
- operator workflow and usage results by permitted role;
- customer-owned operational impact measures and attribution caveats;
- platform reliability, backup/restore and resource measurements;
- customer/data-owner approval and independent QA result.

The scorecard is not pass/fail until the CEO/product owner and pilot customer select the required outcomes and thresholds. The first pilot should produce a trustworthy baseline and measurement process, not an invented savings percentage.
