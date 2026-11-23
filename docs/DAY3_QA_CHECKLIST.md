# Day 3 Independent QA Checklist

**Purpose:** Let a reviewer who did not build the feature reproduce the installation and functional checks, inspect their evidence, and record a clear pass/fail result.
**Application code to reproduce:** `60579a0f4029c4f3b49376abc338d9f34baac0c3` on `main`. This is not yet designated as the release candidate. Repeat these steps against the final reviewed commit before acceptance.

## Demo release gates (all required)

- [ ] **Clean-install rehearsal:** on a fresh laptop or isolated Compose project, use empty PostgreSQL and Ollama model volumes; confirm install, model provisioning, `/ready`, login, dashboard, and restart persistence. Record machine specs and install evidence. Do not alter or reuse the active `anzma-core` project/volumes.
- [ ] **Final regression:** run the approved regression suites against the exact release-candidate commit and an isolated validation database; record commands, results, and artifacts. Historical results from another commit do not satisfy this gate.
- [ ] **Independent walkthrough:** a reviewer who did not implement the release follows this checklist, including install and dashboard workflows, and records Pass/Fail with evidence and open defects.

Detailed procedures are in sections 2–6 below. All three gates must pass before calling the installable demo release-verified.

## 1. Record the test environment

- [ ] Record reviewer, date/time, OS, Docker Desktop/Engine version, CPU/RAM, and available GPU/runtime.
- [ ] Record exact Git commit and working-tree status.
- [ ] Confirm `.env` is local and not included in Git output.
- [ ] Record the Compose project and ports in use; do not stop or replace another active project.
- [ ] Record whether this is a fresh database, fresh Ollama volume, or reused installation.

## 2. Start and verify the application

From PowerShell in the approved repository checkout:

```powershell
Set-Location 'E:\Anzma Working\Anzma Platform Production'
docker compose --env-file .env -p anzma-core --profile freshness -f deploy/compose/docker-compose.yml up -d --build
docker compose --env-file .env -p anzma-core --profile freshness -f deploy/compose/docker-compose.yml ps
Invoke-WebRequest -UseBasicParsing http://localhost:8000/ready
Start-Process 'http://localhost:8000'
```

- [ ] Database and API report healthy/running.
- [ ] Readiness returns HTTP 200 and database `up`.
- [ ] Dashboard opens in the browser with no token prompt.
- [ ] Record any startup warnings/errors and retain relevant logs.

## 3. Record continuous browser demonstration

Start recording before opening the dashboard. Do not cut between the required actions. Avoid exposing secrets.

- [ ] Show HVAC, Network, Access Control, CCTV, and Fire Alarm with current states.
- [ ] Identify at least one warning and one critical condition and explain the visible evidence.
- [ ] Open an incident and inspect its related alerts and source evidence.
- [ ] Acknowledge the incident, then resolve it; show resulting state and relevant counters.
- [ ] Apply a system filter and a time filter; show data changing consistently and restore the default view.
- [ ] Open the AI assistant, ask a relevant operational question, inspect answer source/model and references.
- [ ] Show how recovered, historical, current, and unavailable/stale data are distinguished.
- [ ] Save the recording filename, duration, and checksum in the report package.

## 4. Run automated checks

Record exact command, exit status, test count, and output artifact for each run. Run only against the approved validation database/environment.

```powershell
docker exec anzma-core-api-1 python /app/tests/unit/test_day2_ahmed.py
docker exec -e ANZMA_AUTH_DISABLED=false anzma-core-api-1 python /app/tests/integration/test_frontend_api.py
docker exec anzma-core-api-1 python /app/tests/scenarios/e2e_s1.py --build
docker exec anzma-core-api-1 python /app/tests/scenarios/e2e_s1.py --build --llm
```

- [ ] Unit suite passes on the final commit.
- [ ] Frontend API suite passes on the final commit.
- [ ] Fallback scenario passes with the model unavailable; evidence references resolve.
- [ ] Verify browser chat availability and separately review answer quality using representative operational questions; record prompt, model/source, retrieved facts, citations, correctness, and abstention behavior, including the operator-reported incorrect-answer cases.
- [ ] Live S1 alert-explanation scenario produces an actual model answer with a model identifier. A test that returns fallback does **not** pass this specific live alert-explanation check even if its general scenario assertions pass.
- [ ] Confirm isolated integration/E2E data was not written into the production `anzma` database.
- [ ] Run the remaining approved regression suites on the final commit; historical pre-change counts are not substitutes.

## 5. Inspect data and AI evidence

- [ ] Confirm event, alert, and incident records are distinguished.
- [ ] Trace each sampled incident alert to its stored event/telemetry evidence.
- [ ] Confirm repeated detection does not create duplicate alerts/incidents in the tested scenario.
- [ ] Verify units from stored values through dashboard presentation, especially `bandwidth_util` in `%`.
- [ ] Confirm current faults, recovery, historical incidents, stale/missing, and unknown are not conflated.
- [ ] For live AI, record model ID, source measurements, grounding result, and recommendations; verify each against source evidence.
- [ ] Do not treat a successful chatbot response as proof that the separate S1 alert-explanation path passed; record evidence for both paths.
- [ ] Disable/unavailable model and repeat the same scenario; confirm deterministic fallback and its source label.
- [ ] Confirm provisional recommendations are not presented as formally approved without a real approver and approval reference.
- [ ] Obtain engineering’s written decision before changing bandwidth thresholds.

## 6. Installation rehearsal

- [ ] Use the approved repository and a fresh PostgreSQL volume in an isolated project/ports configuration.
- [ ] Verify migrations complete, API/database readiness succeeds, and persistent storage survives container recreation.
- [ ] Verify the documented Ollama model-provisioning path from an empty model volume, or clearly report why it was not run.
- [ ] Demonstrate deterministic fallback if Ollama/model is unavailable.
- [ ] If GPU is claimed, verify GPU visibility from inside the Ollama container and prove inference uses it; host GPU presence alone is insufficient.
- [ ] If external Ollama is claimed as supported, test endpoint connectivity, model availability, configuration, and failure fallback separately.

## 7. Sign-off record

| Field | Reviewer entry |
|---|---|
| Final Git commit | |
| Environment and install type | |
| Browser recording path / checksum | |
| Automated suite results | |
| Live AI result and model ID | |
| Fallback result | |
| Open defects / deviations | |
| Reviewer decision (Pass / Fail / Conditional) | |
| Reviewer and date | |

Acceptance is complete only when mandatory workflow and installation checks pass, live and fallback AI paths are separately evidenced, operational data is consistent, required regression suites pass on the exact final commit, and no critical/high-severity regression remains.
