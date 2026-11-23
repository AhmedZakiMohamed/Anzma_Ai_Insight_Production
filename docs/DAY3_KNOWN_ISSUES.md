# Day 3 Known Issues and Open Items

**Updated:** 23 November 2026
**Validated application code commit:** `60579a0f4029c4f3b49376abc338d9f34baac0c3` on `main`.
**Release status:** Not ready for final Day 3 acceptance. The code commit exists but is not designated as the release candidate.

## High-priority acceptance gaps

| Priority | Issue | Impact | Required resolution |
|---|---|---|---|
| High | Browser chat returns responses that the operator reports are sometimes incorrect. | A responsive model is not necessarily a trustworthy assistant; users may act on an inaccurate answer. No representative prompt/answer examples have been captured for diagnosis. | Capture incorrect answers with the prompt, retrieved facts, model/source label, and evidence references. Determine whether the failure is retrieval/data selection, model reasoning, or grounding; add these cases to a repeatable answer-quality review and require evidence-backed answers or an explicit abstention. |
| High (feature-specific) | The scripted S1 live alert-explanation path did not complete successfully. | Those runs returned fallback with `model_used=None`. The team reports Qwen works in browser chat on a teammate's laptop; that does not establish that the separate S1 explanation path works. | Preserve both results as separate paths. Record the teammate's working model/runtime configuration. Diagnose and rerun S1 only if live alert explanations are part of the release acceptance scope. Keep deterministic fallback available. |
| High | No continuous browser demonstration recording has been supplied for review. | The operator reports the browser workflow works, but the CEO’s recorded-demo acceptance cannot be independently checked. | Record and provide one continuous browser run covering dashboard, incident actions/counters, filters, assistant, and supporting evidence. |
| High | Clean-install rehearsal remains open. | Current run used the active installation and does not prove first-start behavior from empty database and model volumes. | Run the installer on a fresh laptop or isolated Compose project with empty volumes; capture provisioning, readiness, login, and restart-persistence evidence without touching active `anzma-core` data. |
| High | Final regression on a release candidate remains open. | Previous suites are tied to earlier commits; the current changes are uncommitted and results are not bound to one reproducible release revision. | Select and record the exact candidate commit, run approved regression suites in an isolated validation environment, fix failures, and record outputs against that commit. |
| High | Independent installation and dashboard walkthrough remains open. | The team has not supplied an independent reviewer result for the current release candidate. | Have a reviewer who did not implement the release follow `DAY3_QA_CHECKLIST.md` and record Pass/Fail, evidence, and deviations. |

## Deployment and environment limitations

| Priority | Issue | Impact | Required resolution |
|---|---|---|---|
| Medium | Empty Ollama-volume model provisioning was not rehearsed. The clean-checkout rehearsal reused a model volume. | Clean model download/init behavior is not yet proven from zero state. | Perform an approved isolated clean-volume installation, including model provisioning and readiness, without disturbing the active project. |
| Medium | GPU passthrough to the Ollama container is unverified. | Host GPU presence does not establish container acceleration; CPU model loading exceeded the application request window. | Verify Docker Desktop’s supported GPU runtime and Ollama device visibility; record the evidence. Do not change active Compose GPU settings without validation. |
| Medium | External Ollama endpoint configuration was not independently exercised. | That deployment option’s network/model requirements are documented but not verified here. | Validate it in an isolated environment if it remains a supported configuration. |
| Medium | Installation rehearsal occurred on the same Windows/Docker host, not a second clean machine or independent QA system. | Reproducibility across a separate installation environment is outstanding. | Have independent QA follow the documented install procedure on the approved test environment. |

## Operational data and validation limitations

- The dashboard’s current dataset is seed/legacy data. No real vendor or sensor source is registered or connected.
- Freshness records showed no persisted series snapshot and no registered ingestion sources; the panel’s missing/stale labels must not be described as confirmed equipment outages.
- The recorded baseline counts and bandwidth measurements in `DAY3_DATA_VALIDATION.md` are from a prior local snapshot and must be re-queried on the final release candidate.
- Bandwidth values are percentages. The recorded thresholds have reported verbal approval but no written approval artifact was found. No threshold behavior was changed.
- The current unsupported-causality guard is narrow; grounding checks do not guarantee semantic correctness.
- Earlier 43/43 and 23/23 suite results predate the latest Task 4 edits and require rerun before release.

## Not in this sprint

Do not include vendor SDK development, KNX integration, a new historian, workflow-engine development, or milestone integration in this Day 3 closeout. No live ingestion pipeline is claimed by these reports.
