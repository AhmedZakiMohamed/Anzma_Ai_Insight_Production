# Anzma AI Insight Architecture Decision Register

**Status:** Open working register; no decision in this document is represented as CEO-approved.  
**Owner:** Amr / product owner unless an approver is named below.  
**Companion:** [`WHOLE_SOLUTION_ARCHITECTURE.md`](WHOLE_SOLUTION_ARCHITECTURE.md)

Use this register to keep business and engineering choices visible as the architecture develops. A recommendation is not a decision. Mark an item **Decided** only after the authorized owner confirms it; then record the decision date, approver, rationale, and any conditions. Revisit a decision if customer evidence invalidates its assumptions.

## Decision order

Resolve decisions in dependency order. Hosting and the pilot determine network, user identity, isolation, data, operations, and sizing constraints. Avoid finalizing detailed deployment specifications before the earlier choices and pilot evidence exist.

| ID | Priority | Decision | Status | Decision owner | Blocks / affects |
|---|---:|---|---|---|---|
| ADR-01 | 1 | Launch hosting promise and who operates deployment | Open | CEO / product owner | Tenant boundary, network, identity, support, packaging, backup, sizing |
| ADR-02 | 2 | First real pilot customer/site and source system | Open | CEO + integration owner | Connector scope, device/source requirements, validation fixtures, demo |
| ADR-03 | 3 | Customer/building isolation model | Open | Product + security/architecture | Database model, authorization, deployment topology, SaaS feasibility |
| ADR-04 | 4 | Operator identity and role model | Open | Product + customer IT/security | API/UI auth, permissions, audit, personnel analytics |
| ADR-05 | 5 | First-release data categories and privacy policy | Open | CEO + customer data owner | Connector fields, analytics catalog, retention, privacy controls |
| ADR-06 | 6 | Connector deployment and support tiers | Open | Product + integration owner | Installer, network paths, operations, commercial claims |
| ADR-07 | 7 | Analytics definition and approval ownership | Open | Data/analytics lead + business owner | KPI catalog, deterministic logic, acceptance tests and reports |
| ADR-08 | 8 | Data retention, availability, backup and recovery targets | Open | Product + operations/customer IT | Database sizing, storage, monitoring, release qualification |
| ADR-09 | 9 | Local or remote AI provider and supported workload | Open | Product + architecture/security | Model container, CPU/GPU/RAM, privacy, outage behavior |
| ADR-10 | 10 | Product naming and visual/deployment claim | Open | CEO / brand owner | Blueprint language, UX, website, customer demonstrations |
| ADR-11 | 11 | Sensitive-alert recommendation and AI authority | Open | CEO / product owner + operations/data owner | Recommendation display, model prompt/tool permissions, alert UI, approval policy, audit |
| ADR-12 | 12 | Structured analytics only or add knowledge retrieval (RAG/GraphRAG) | Open | Product + AI/data architecture | AI scope, indexing pipeline, sources/permissions, token and storage budget, answer quality |
| ADR-13 | 13 | Platform metrics/observability stack (including Graphite) | Open | Platform/operations owner | Host and service monitoring, metric retention, deployment footprint, support |

## Decision details

### ADR-01 — Launch hosting promise and operator

**Question:** Is the first sellable release customer-managed on-premises/private cloud with Core installed once and browser access, Anzma-managed SaaS, or both?

**Options:**

- **A. Customer-managed on-premises/private cloud first.** Core is installed once on a supported server or VM. Users access through a browser; supported embedding is optional. Anzma may assist setup and upgrades.
- **B. Anzma-hosted SaaS first.** Anzma operates the service and customer sites connect to it through approved secure paths.
- **C. Both in the first release.** Ship distinct deployment profiles and operate both models from launch.

**Working recommendation:** Start with A unless the CEO's launch customer or commercial plan requires hosted SaaS. It best matches the written install-once promise and avoids asserting that customer isolation and SaaS operations are already designed. Do not position a private-cloud deployment as SaaS by default.

**Trade-offs:** A increases customer IT and upgrade coordination but keeps data under customer control. B centralizes upgrades/support but requires a tenant isolation, connectivity, privacy, availability and service operations design. C broadens reach but multiplies qualification, support and security work.

**Evidence needed:** target customer deployment constraint, customer data-residency/security requirement, who owns the host/network, expected commercial packaging, and a customer-like install walkthrough.

### ADR-02 — First real pilot and source

**Question:** Which real building system and site will validate the first connector after the synthetic reference service?

**Options:** BMS/BACnet or OPC UA gateway; access control API; VMS metadata API; metering/IoT platform; another named source selected by the pilot customer.

**Working recommendation:** Select one source for which the team can obtain authorized API documentation, a test endpoint, a read-only credential, realistic historical data and a customer technical contact. Choose by access to evidence and customer value, not visual prominence in the concept images.

**Trade-offs:** Access control may prove meaningful event analytics but brings identity/privacy concerns. BMS may offer rich points but needs careful semantic mapping. VMS brings vendor licensing and recording-health distinctions. Metering is valuable for energy analytics but needs interval and baseline rules.

**Evidence needed:** vendor/model/version, API/SDK or gateway details, permission list, representative payloads, history and rate limits, timestamp behavior, endpoint/network plan, and customer data approval.

Capture these facts in [`PILOT_SYSTEM_QUALIFICATION_TEMPLATE.md`](PILOT_SYSTEM_QUALIFICATION_TEMPLATE.md); do not mark a source supported until its capability and acceptance evidence are completed.

### ADR-03 — Customer and building isolation

**Question:** Can multiple customers/buildings share one Core database and service in the first supported release?

**Options:** Separate Core/database per customer; shared Core with database/schema isolation; shared tables with enforced organization/building scope.

**Working recommendation:** For initial customer pilots, prefer a separately deployed Core/database per customer until tenancy, authorization and migration behavior are designed and independently tested. This is a deployment recommendation, not a commitment to a permanent product limit.

**Trade-offs:** Separate deployments are easier to reason about for isolation but increase operations cost. Shared service improves operating efficiency but increases the consequence of scope bugs and requires row-level or equivalent enforcement throughout APIs, analytics, dashboards, reports and exports.

**Evidence needed:** expected customer count, service ownership, cross-building reporting requirements, isolation/regulatory obligations, deployment operations capacity, and a verified negative cross-customer test plan.

### ADR-04 — Operator identity and role model

**Question:** How are human users authenticated, authorized, deprovisioned and audited?

**Options:** OIDC/SSO at a trusted reverse proxy; application-managed accounts with roles; shared bearer token for isolated demo only.

**Working recommendation:** For production multi-user operation, use customer identity (OIDC/SSO or an explicitly approved equivalent) and an application authorization layer that enforces role and resource scope. Keep the shared token only for isolated demo/system use. Connector credentials remain separate from operator identity.

**Trade-offs:** Proxy/SSO uses existing customer lifecycle but needs a trusted network boundary and identity propagation. Managed accounts require account recovery, MFA policy and support. A shared token cannot attribute actions or enforce personal permissions and blocks personnel-level analytics.

**Evidence needed:** customer IdP availability, user roles, deprovisioning expectations, audit fields/retention, offline behavior, MFA/compliance requirements and API-client identity needs.

### ADR-05 — First-release data categories and privacy

**Question:** Are first-release analytics limited to building/equipment operations, or do they include people, attendance or access identities?

**Working recommendation:** Start with equipment/building events and anonymous or non-personal metrics. Keep person identity and attendance fields disabled until customer purpose, lawful basis/policy, user role, minimization, retention, export handling and audit are approved.

**Trade-offs:** Personnel use cases can deliver value but materially change authorization, data minimization, customer onboarding, retention, and harm from misuse or incorrect inference.

**Evidence needed:** customer-approved use cases, fields, purpose, authorized roles, retention/deletion, identity mapping source, review process and unacceptable inference rules. An access swipe alone should not establish attendance or lateness.

### ADR-06 — Connector deployment and support tiers

**Question:** Which connector types can customers install themselves, and which require configured protocol/API work or a custom integration?

**Working recommendation:** Publish three support tiers consistent with the blueprint: certified self-service connector; configurable protocol/API connector; custom integration required. State separately where each connector runs and who upgrades it. Never imply one-click support for every BMS/VMS.

**Evidence needed:** qualify each connector by source product/version and named capability, not only by vendor. The [architecture support-tier criteria](WHOLE_SOLUTION_ARCHITECTURE.md#customer-facing-connector-support-tiers) require source-version coverage, network requirements, permissions, discovery/mapping accuracy, restart/recovery, upgrade compatibility and an independent customer-like setup walkthrough. At least one real source must pass before claiming a real vendor connector is certified; the synthetic reference connector does not qualify.

### ADR-07 — Analytics semantic ownership

**Question:** Who approves formulas, prerequisites, time semantics, confidence/coverage, evidence and limitations for each analytic?

**Working recommendation:** Assign a named data/analytics owner and business approver per analytic family. Store definitions/version with tests and use the same result contract for chat, dashboard and reports. Product examples are not automatic approval of formulas.

**Evidence needed:** signed-off definitions and fixtures reconciled to source records, boundary cases, missing-data cases, timezone rules, permission behavior, evidence fields and versioning/backward-compatibility plan.

### ADR-08 — Retention and service objectives

**Question:** What data and artifact retention, uptime, alert latency, source buffering, RPO, RTO and support window should Anzma promise?

**Working recommendation:** Do not publish numerical SLAs or fixed retention until pilot measurements and customer operating expectations are captured. Define separate targets for source polling, data freshness, alert delivery, report retention and platform availability.

**Evidence needed:** customer impact classification, observed event rates and data growth, source API limits, outage duration, backup destination, restore drill, support responsibility and monitoring coverage.

**Current planning input, not an approved decision:** the scenario proposal assumes 3,000 points per site, 5-minute reporting and 90 days online. The estimated 77.76 million rows depends on 3,000 being the total site point count. Confirm scope, retention, actual row/index/WAL growth and restore objectives in the pilot qualification before using it for capacity or SLA claims. See [`WHOLE_SOLUTION_ARCHITECTURE.md` §11](WHOLE_SOLUTION_ARCHITECTURE.md#working-planning-profile-for-scenario-comparison).

### ADR-09 — AI provider and local inference

**Question:** Must every customer environment run an on-premises model, or may deployments use an approved external provider?

**Working recommendation:** Keep provider selection replaceable and make AI optional to analytics correctness. Decide per deployment based on data policy, offline needs, latency/concurrency and model hardware. The deterministic analytics and fallback must operate without model availability.

**Evidence needed:** allowed data egress, offline requirement, model/provider approval, target response time, concurrent use, model size/license and measured CPU/GPU/RAM utilization.

**Current planning input, not an approved decision:** compare a 7–8B Q4 Arabic/English model and 2–4 simultaneous responses against the current Qwen2.5:3b Compose default. Treat the 20–60 second CPU and 3–8 second GPU answer-time figures as unverified estimates. Keep a 16–24 GB GPU as a candidate tier until bilingual quality, concurrency, latency, memory and failure behavior are benchmarked. See [`WHOLE_SOLUTION_ARCHITECTURE.md` §11](WHOLE_SOLUTION_ARCHITECTURE.md#working-planning-profile-for-scenario-comparison).

### ADR-10 — Product name and visual promise

**Question:** Which name and deployment representation are approved for customer materials?

**Working recommendation:** Use Anzma AI Insight in working architecture until the CEO/brand owner approves a name. Align visuals on “Core installed once; browser access; embedding only where supported.” Treat current images as concepts, not screenshots.

**Evidence needed:** approved brand/name, deployment promise, supported integration tiers, customer UX review and correction of per-PC-install/SaaS statements that do not apply to the selected model.

### ADR-11 — Sensitive-alert recommendation and AI authority

**Question:** For high-impact alerts, does Anzma show only one deterministically selected, formally approved next action, and what may the AI do around that action?

**User direction from earlier Anzma discussions:** prefer a clear deterministic solution for sensitive alerts over a free-form model recommendation. The current CEO blueprints demonstrate alert investigation, acknowledgement and work-order workflow, but do not fully specify whether a model may select or rank corrective actions. This user preference is therefore a strong design input, not a recorded CEO approval.

**Options:**

- **A. Deterministic action, AI explanation only (recommended):** an approved playbook/rule selects the single next action from validated evidence; the model explains the alert and cites facts but cannot choose or change the action. If no approved action exists, show the approved verification/escalation state.
- **B. Deterministic eligible set, model selects:** rules restrict eligible actions and the model chooses among them. This remains model-dependent behavior and can make the action differ between otherwise equivalent explanations.
- **C. Model-generated recommendation:** the model proposes actions from context. This has the highest safety and validation burden and is not consistent with the stated user preference for sensitive alerts.

**Working recommendation:** Choose A for sensitive alerts. Keep recommendation text concise; show evidence, confidence/limitations and approval status separately. Do not surface provisional playbook entries as approved actions. Acknowledge, create work order or other workflow changes only after an authorized operator action; never let the model control building equipment.

**Current-code gap:** `app/domain/ai/explanation.py` sends eligible causes/actions to the model, accepts model-selected IDs and falls back to all eligible actions when no action ID is selected. `app/domain/ai/recommendations.py` includes both `APPROVED` and `PROVISIONAL` actions in its returned recommendation set, although `is_approved` separately requires a real approver and approval reference. Existing playbook defaults are provisional. This source behavior does not yet meet option A.

**Evidence needed:** product/operations owner approval of the recommendation policy; a documented severity/action matrix and approved playbooks; tests proving the chosen action is deterministic across model output/failure and that provisional actions cannot be represented as approved; operator confirmation and audit evidence for work-order/acknowledgement actions.

### ADR-12 — Knowledge retrieval for AI answers

**Question:** Do first-release operational questions need approved unstructured documents or graph relationships in model context, or are structured analytics and evidence sufficient?

**Options:** Keep first release on structured, deterministic analytics; add conventional document retrieval; or evaluate GraphRAG for questions that need relationships across approved manuals, procedures and asset/vendor knowledge.

**Working recommendation:** Do not make GraphRAG a launch dependency until a reviewed question set shows it improves answers beyond authorized SQL results and curated citations. If evaluated, keep retrieved knowledge separate from live telemetry facts, enforce source permissions and versioning, cite retrieved passages, and bound the context sent to the model. GraphRAG indexing may use LLM calls and query-time retrieved context increases token use; measure both.

**Evidence needed:** questions not served by current analytics; approved source-document set and permissions; Arabic/English retrieval evaluation; citation and version tests; indexing and query token cost; index storage, memory, rebuild and update process; behavior when a source is stale or unavailable.

### ADR-13 — Platform metrics and Graphite

**Question:** Which operational metrics and retention are required, and should the platform add Graphite or another metrics backend?

**Working recommendation:** First define the signals needed for Core health: host/container CPU and RAM, disk, API/database health, connector sync status, model latency/queue and backup outcomes. Keep this observability data separate from building telemetry and from the PostgreSQL product historian. Graphite is an optional monitoring stack, not an AI model; it does not consume LLM tokens unless its values are explicitly included in prompts. It is not currently deployed in Compose.

**Evidence needed:** required dashboards/alerts, metric names and cardinality, sampling interval, retention/downsampling, resource budget, deployment owner, access controls and support/restore plan. Compare candidate backends against these requirements before selecting one.

## Decision log

| ID | Status | Approver | Date | Decision / conditions | Evidence or follow-up |
|---|---|---|---|---|---|
| ADR-01 through ADR-13 | Open | — | — | No architecture/business decision is recorded as approved here. | Review in priority order; update individual entries when confirmed. |
