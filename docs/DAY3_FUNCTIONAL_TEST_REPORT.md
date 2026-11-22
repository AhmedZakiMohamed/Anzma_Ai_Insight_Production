# Day 3 Functional Test Report

**Report date:** 22 November 2026
**Product:** Anzma AI Insight Core v0.1 — Consolidated Working Release
**Checkout:** `main`, source base `455a185afda5704b83c2b1b2223e63529de06cef`
**Release candidate:** Not created. The checkout contains uncommitted changes; this hash identifies the starting commit, not the tested final tree.

## Executive result

The operator reports that the browser workflow is working and has completed the requested dashboard demonstration. The current checkout has also passed the latest recorded backend unit and frontend API suites, a deterministic-fallback S1 scenario, and a live-model S1 attempt that safely fell back. The live Qwen acceptance is **not passed**: the model did not return within either the default or extended request window. A video recording has not been provided or found in the repository, so the demonstration evidence is outstanding for independent review.

This is a working validation report, not a release sign-off. The reports and evidence are tied to the source base above, but the final dirty worktree has no immutable commit identifier.

## Scope and tested environment

- Existing local Windows/Docker Compose environment, project `anzma-core`.
- Dashboard/API: `http://localhost:8000`; Ollama: `http://localhost:11434`; PostgreSQL remains on the existing Compose configuration.
- Main application database: `anzma`. The S1 scenarios used a separate temporary `anzma_e2e` database, which was dropped after the runs.
- Configured model: `qwen2.5:3b` (reported by Ollama as 1.9 GB).
- No vendor SDK, KNX, historian, workflow engine, or new milestone integration was added.

## Acceptance results

| Requirement | Result | Evidence / note |
|---|---|---|
| Show all five building systems and current statuses | Operator reports passed | Current seeded dashboard shows HVAC, Network, Access Control, CCTV, and Fire Alarm. Recorded video was not supplied for review. |
| Identify critical and warning conditions | Operator reports passed | Current dashboard shows warning and critical states; stale/missing data indicators are separate. |
| Open incident and inspect alerts/evidence | Operator reports passed | S1 scenario checks incident evidence links; 14 of 14 evidence references resolved to stored source records. |
| Acknowledge and resolve incident | Operator reports passed | Browser demonstration reported complete; no recording artifact available for independent confirmation. |
| Verify counters update | Operator reports passed | Browser demonstration reported complete; no recording artifact available for independent confirmation. |
| Apply system/time filters and verify consistency | Operator reports passed | Browser demonstration reported complete; no recording artifact available for independent confirmation. |
| Ask assistant an operational question and inspect references | Partially verified | Fallback produced an explanation with available supporting evidence. A successful live Qwen answer was not obtained. |
| Distinguish faults, recovered, historical, unavailable data | Operator reports passed; data checks partial | Current dashboard distinguishes warning/critical/no-data and freshness. Full browser recording is still needed for review. |
| Live AI explanation from Qwen with model/source/grounding/recommendations | **Not passed** | Live S1 requests returned `ai_source=fallback`, `model_used=None`. Ollama logs show model load exceeded the request window and the client request was cancelled. |
| Same scenario with model disabled / deterministic fallback | **Passed** | `tests/scenarios/e2e_s1.py --build`: 14/14 checks passed; all 14 evidence references resolved. |

## Recorded checks on the current working tree

| Check | Result | Scope |
|---|---:|---|
| `tests/unit/test_day2_ahmed.py` | 29/29 passed | Latest recorded run after the current Task 4 safety changes. |
| `tests/integration/test_frontend_api.py` | 42/42 passed | Latest recorded run after the current Task 4 changes, with test authentication configured for that process. |
| `tests/scenarios/e2e_s1.py --build` | 14/14 passed | Deterministic fallback path; isolated `anzma_e2e` DB. |
| `tests/scenarios/e2e_s1.py --build --llm` | Scenario checks passed but live AI acceptance failed | Both default timeout and a process-only `OLLAMA_TIMEOUT=360` attempt used fallback; this is not a live-model pass. |
| API readiness | HTTP 200 | `/ready` returned `{"status":"ready","database":"up"}` in the running Compose environment. |

Earlier runs of other suites (43/43 and 23/23) were made before the latest Task 4 edits. They are historical evidence and have not been rerun against the current dirty tree; they are not counted as current regression passes.

## AI safety behavior

The current work makes corrective recommendations provisional unless a real approval record contains an approver and approval reference. Draft actions are excluded from recommendation output. The answer validator rejects several explicit unsupported-causality phrases, but this is a narrow safeguard and not a complete semantic verifier. A passed grounding check means the configured evidence checks passed; it does not prove that every sentence is semantically correct.

## Outstanding before acceptance

1. Diagnose Ollama model-load latency and verify whether Docker Desktop passes the host GPU through to the Ollama container. Do not treat the host GTX 1650 as proof that the container is using it.
2. Repeat the same live-Qwen S1 scenario and record model identifier, source measurements, grounding result, answer, and recommendations only after a real model response is returned.
3. Supply and review one continuous browser recording covering the required workflow and the live/fallback AI paths as required by the CEO acceptance criteria.
4. Rerun the full approved regression suite against the final tree, record results, and tie the release package to a new clean Git commit.
5. Have an independent QA reviewer follow `DAY3_QA_CHECKLIST.md`.

## Reproduction commands

From the repository root, in PowerShell:

```powershell
docker exec anzma-core-api-1 python /app/tests/unit/test_day2_ahmed.py
docker exec -e ANZMA_AUTH_DISABLED=false anzma-core-api-1 python /app/tests/integration/test_frontend_api.py
docker exec anzma-core-api-1 python /app/tests/scenarios/e2e_s1.py --build
docker exec anzma-core-api-1 python /app/tests/scenarios/e2e_s1.py --build --llm
```

Run the integration suite only when its configured isolated test database is available; it creates and removes test data in that database. The live command above is a reproduction attempt, not a passing acceptance result.
