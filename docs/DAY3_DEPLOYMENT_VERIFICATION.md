# Day 3 Deployment Verification

**Report date:** 22 November 2026
**Validated application code commit:** `60579a0f4029c4f3b49376abc338d9f34baac0c3` on `main`
**Status:** Local Compose stack is operational. Clean database/checkout installation was rehearsed. A clean model-volume provisioning run and independent-machine installation remain unverified. This commit is not designated as a release candidate because acceptance work remains open.

## Supported local startup used for this verification

The active deployment is the repository Compose project `anzma-core`; keep its existing project name, ports, and persistent volumes. From PowerShell:

```powershell
Set-Location 'E:\Anzma Working\Anzma Platform Production'
docker compose --env-file .env -p anzma-core --profile freshness -f deploy/compose/docker-compose.yml up -d --build
Invoke-WebRequest -UseBasicParsing http://localhost:8000/ready
Start-Process 'http://localhost:8000'
```

The readiness probe returned HTTP 200 with `{"status":"ready","database":"up"}`. The user also reported the full browser workflow working. This report does not claim the dashboard recording has been independently reviewed.

## Package contents reviewed

| Component | Status | Notes |
|---|---|---|
| FastAPI application and dashboard | Present and running | API exposed on the existing port 8000. Dashboard uses the application served by the API. |
| PostgreSQL and migrations | Present and ready | Existing application DB is `anzma`; `/ready` reports database up. Migration rehearsal on a fresh DB completed through migrations 006–011. |
| Ollama runtime | Present and healthy | Existing service is available on port 11434. |
| Configured Qwen model | Present in active model volume | `qwen2.5:3b`, 1.9 GB reported by `ollama list`. |
| Persistent storage | Configured in Compose | PostgreSQL and Ollama use persistent named volumes. The clean-clone rehearsal reused an already-provisioned Ollama model volume. |
| Environment configuration | Required | Local `.env` is used. Keep secrets local; do not commit it. `.env.example` documents the variable names. |
| API/database readiness | Verified | `/ready` returned 200 and database status `up`. DB container reported healthy. |
| Freshness worker | Running when the `freshness` profile is enabled | Existing service; no external sensor source has been connected. |
| Deterministic AI fallback | Verified | S1 fallback path succeeded with all 14 evidence links resolved. |
| Browser chat using Qwen | Team-confirmed working on teammate's laptop | User reports Qwen works in browser chat on a teammate's laptop. Exact model/runtime configuration and independent reproduction are not captured in this evidence package. |
| S1 live alert-explanation inference | Not verified successfully | This separate scripted path exceeded the request window in two attempts and used fallback. |

## Installation rehearsal

A clean checkout at the repository’s local `main` commit was run with a fresh PostgreSQL volume and isolated host ports (API 18011, DB 15450, Ollama 11441). The database migrated through 006–011, readiness returned 200, the model was visible, and the model-init service exited successfully. This verified a fresh database and checkout on the same Windows/Docker host.

Limit: the rehearsal reused an already-provisioned Ollama model volume to avoid downloading the model a second time. Therefore it did not prove model provisioning from a completely empty Ollama volume. It was not performed on a second physical machine and was not an independent engineer’s reproduction.

## Runtime and data boundaries

- Main Compose project and existing user ports were retained. Do not start a second project against the same host ports.
- The S1 tests used a temporary `anzma_e2e` database in the running PostgreSQL container and removed it after testing. The application’s `anzma` database was not the S1 test target.
- The dashboard’s current operational data is seed/legacy data. There is no registered live ingestion source or connected external building sensor in this verification.
- The team reports that Qwen works in browser chat on a teammate's laptop. The exact model/runtime configuration and independent reproduction are not captured here. Compose service health alone does not prove inference behavior.
- The scripted S1 alert-explanation path remains unresolved: it exceeded the request window and used fallback in both recorded attempts. This does not negate the separate browser-chat success.
- Windows host reported an NVIDIA GeForce GTX 1650 with 4 GB. GPU passthrough to Docker/Ollama was not verified; Ollama logs showed CPU work during the long model load.

## External Ollama configuration

The repository can be configured to call an Ollama endpoint outside the Compose-managed runtime through environment configuration. This is a supported deployment option only when the endpoint is reachable from the API container, the requested model is installed there, and network/security settings permit access. This environment’s external-Ollama path was not independently tested. The team reports successful Qwen use in browser chat on a teammate's laptop, while the separate S1 alert-explanation inference fell back in the recorded attempts.

## Known installation limitations

1. A completely empty Ollama volume/model-download path has not been rehearsed end to end.
2. The scripted S1 live alert-explanation path returned deterministic fallback after model loading exceeded the request window. This is distinct from the team-reported successful browser chat. Reproduce and review both paths separately if both are in release scope.
3. Docker GPU access is unverified. Do not infer container GPU acceleration from the Windows host GPU listing.
4. No independent clean-machine install or recorded demonstration was supplied for review.
5. Commit `60579a0` identifies the current application source, but it is not designated as the final release candidate because acceptance work remains open.

## Reproduction and operator commands

```powershell
Set-Location 'E:\Anzma Working\Anzma Platform Production'
docker compose --env-file .env -p anzma-core --profile freshness -f deploy/compose/docker-compose.yml up -d --build
docker compose --env-file .env -p anzma-core --profile freshness -f deploy/compose/docker-compose.yml ps
Invoke-WebRequest -UseBasicParsing http://localhost:8000/ready
Start-Process 'http://localhost:8000'
```

To stop services without deleting persistent volumes:

```powershell
docker compose --env-file .env -p anzma-core --profile freshness -f deploy/compose/docker-compose.yml down
```

Do not add `-v` to `down` for routine shutdown; that would remove persistent data volumes.
