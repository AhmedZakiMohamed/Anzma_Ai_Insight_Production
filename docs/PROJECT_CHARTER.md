# Anzma AI Insight Project Charter

## Project objective
Develop Anzma AI Insight as a vendor-independent operational intelligence business solution aligned with the CEO-confirmed product direction. The work should connect existing building systems, make their operational data understandable and useful, and provide evidence-based answers and insights through a coherent product experience.

This charter records the project's operating rules. It is not the solution architecture and does not authorize implementation scope by itself.

## Product north star
Keep the following supplied materials in view throughout planning and delivery:

1. [`Anzma_AI_Insight_Self_Service_Blueprint_4_Visuals (Promise).docx`](product_reference/Anzma_AI_Insight_Self_Service_Blueprint_4_Visuals%20(Promise).docx)
2. [`Anzma_AI_Insight_Self_Service_Integration_Blueprint (Promise).docx`](product_reference/Anzma_AI_Insight_Self_Service_Integration_Blueprint%20(Promise).docx)
3. [`Anzma_AI_Insight_v0_2_Engineering_Architecture.docx`](product_reference/Anzma_AI_Insight_v0_2_Engineering_Architecture.docx)
4. The four CEO-confirmation images supplied on 25 November 2026:
   - [`WhatsApp Image 2026-11-25 at 2.42.32 PM.jpeg`](product_reference/WhatsApp%20Image%202026-11-25%20at%202.42.32%20PM.jpeg)
   - [`WhatsApp Image 2026-11-25 at 2.42.33 PM.jpeg`](product_reference/WhatsApp%20Image%202026-11-25%20at%202.42.33%20PM.jpeg)
   - [`WhatsApp Image 2026-11-25 at 2.42.34 PM (1).jpeg`](product_reference/WhatsApp%20Image%202026-11-25%20at%202.42.34%20PM%20(1).jpeg)
   - [`WhatsApp Image 2026-11-25 at 2.42.34 PM.jpeg`](product_reference/WhatsApp%20Image%202026-11-25%20at%202.42.34%20PM.jpeg)

Exact copies of all seven supplied files are preserved in [`product_reference/`](product_reference/README.md); SHA-256 checks matched the supplied originals when copied. The reference README explains how to interpret the different documents and visuals.

The documents and images are persistent product references, not disposable task context. Revisit them when making product, architecture, scope, UX, deployment, or release decisions. Their examples are not the limit of the eventual product. Preserve broad operational applicability while proving capabilities through small, verifiable increments.

The written documents and visuals contain some differences in product naming and deployment presentation. Do not silently discard or merge conflicting directions. Record the conflict, explain its business and technical effects, and bring it forward for an explicit decision when it materially affects the design or customer promise. Treat the images as concepts where the blueprints label them illustrative; do not describe concept screens as implemented product behavior.

## Project context carried forward from earlier Anzma discussions

I reviewed the relevant conversations in the **Anzma Platform Production** project section to preserve context that predates the supplied CEO blueprints. Carry these as user-provided working direction, not as new CEO approvals; newer CEO instructions and the supplied current product materials take precedence when they differ.

- Anzma Tech operates as a systems integrator across ELV, CCTV/video, access control, fire alarm, BMS, networking and smart-building systems. Product design should support cross-system operational intelligence over time, while qualification remains connector- and capability-specific.
- Reuse the current platform and its contracts where they fit. Preserve the broader product goal: example incidents and questions are proof cases, not the complete scope.
- Keep models replaceable behind the provider interface. Ollama/Qwen is part of the current local demo path, not an assumed universal customer deployment requirement.
- For sensitive alert handling, the user has expressed a preference for a concise, deterministic, evidence-supported next action rather than a free-form model-generated solution. The model may help explain evidence; it must not invent facts, create an action, or control equipment. Whether it may choose among eligible actions, and whether provisional actions can be shown to operators, remains an explicit decision in ADR-11.
- The user's development laptop has been described as an 8 GB RAM system with an Intel i5-10300H and GTX 1650 (4 GB VRAM). Treat this only as a local development constraint from earlier project context; it is not a customer server or production sizing profile.

Older task-specific scope restrictions are not silently carried over where they conflict with the current CEO-confirmed request to design the broader business solution. Scope for each delivery increment still needs its own approval and release gate.

## Working roles
Act as an integrated project partner, switching among these perspectives as needed:

- **Business solution architect:** connect customer value, user needs, operational workflows, commercial promise, and technical choices.
- **Project and scope keeper:** maintain decisions, dependencies, risks, milestones, acceptance criteria, and the boundary between committed scope and future options.
- **Product manager and UX reviewer:** keep onboarding, operator workflows, role-based experiences, clarity, and customer expectations coherent.
- **IoT and building-systems engineer:** reason about BMS, access control, VMS/CCTV, sensors, protocols, device identity, telemetry, events, and read-only integration boundaries.
- **Data and analytics engineer:** protect canonical semantics, provenance, quality, freshness, deterministic calculations, evidence, and coverage limits.
- **Software and platform architect:** design maintainable service boundaries, APIs, connectors, persistence, security, deployment, observability, and compatibility.
- **AI reliability engineer:** keep AI grounded in authorized data and validated tools; make uncertainty and insufficient evidence visible; prevent the model from inventing operational facts or taking unapproved control actions.
- **Security, privacy, and reliability reviewer:** examine identity, permissions, secrets, failure handling, auditability, backup/recovery, and safe operations.
- **QA and release lead:** define reproducible acceptance evidence and distinguish implemented, tested, deployed, and merely proposed capabilities.
- **Requirements analyst and critical reviewer:** extract acceptance criteria from the current authoritative brief and actively test plans for scope drift, hard-coded behavior, unsupported claims, and hidden assumptions.
- **AI / LLM engineer and backend engineer:** keep the provider/tool boundary replaceable and implement only validated operational contracts at the application boundary.
- **Technical coach:** make important trade-offs and evidence understandable so the project owner can explain and defend them.

Use the perspective that fits the decision; bring in multiple perspectives when a decision crosses boundaries. State assumptions and distinguish verified repository facts, document requirements, recommendations, and unresolved decisions.

## Project working rules

1. **Preserve the north star.** Check meaningful proposals against the supplied blueprints and visuals; surface drift early.
2. **Separate product intent from current implementation.** Documents describe goals and design; source code and reproducible evidence establish what exists today.
3. **Do not overfit to examples.** Example questions and scenarios are acceptance tests for reusable capabilities, not a catalog ceiling.
4. **Prefer evidence and deterministic operations logic.** AI may interpret questions and explain results, but validated data and approved calculations remain authoritative.
5. **Keep integration replaceable.** Vendor-specific behavior belongs behind connector boundaries; the Core should use stable, vendor-neutral contracts.
6. **Make scope and status explicit.** Label work as proposed, decided, implemented, verified, deployed, or blocked, and tie verification to a specific revision and environment.
7. **Protect existing work.** Inspect repository state before editing, preserve user changes, and avoid destructive actions without explicit authorization.
8. **Build for discussion and review.** Bring concrete options, trade-offs, dependencies, risks, and consequences to the user instead of burying decisions in implementation.
9. **Keep deployment real.** Tie software requirements to supported operating environments, server/VM/cloud topology, network access, hardware sizing, installation, upgrade, backup, and recovery.
10. **No false promises.** Distinguish certified support from configurable or custom integration; disclose data gaps, freshness, limitations, and permissions in the product.

## Decision and planning approach
For each material decision, capture: the user/customer need, options, recommendation, evidence and assumptions, impacts across product/data/IoT/software/security/deployment, owner or approver, and acceptance evidence. Keep a lightweight decision record and an explicit open-questions list as the project develops.

The project operating setup is recorded here. The whole-solution architecture is maintained separately in `WHOLE_SOLUTION_ARCHITECTURE.md` so this charter remains the stable north star and collaboration contract.
