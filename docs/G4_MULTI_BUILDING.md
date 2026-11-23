# G4 — multi-building proof: what is done, what is unverified, how to verify

**Status: IMPLEMENTED IN THE WORKING TREE, VERIFICATION PENDING.** This note was written without running Docker or PostgreSQL. Nothing here is a test result except where a command and its output are quoted. G4 is DONE only when section 3 passes on a fresh disposable stack.

## 1. What the code now does (additive; omitted filter = unchanged behaviour)

| Area | Change | File |
|---|---|---|
| Dashboard | Optional `building` (a `building_code`) on `/dashboard/overview`, `/trends`, `/equipment-health` | `app/api/frontend_api.py` |
| Alerts | Optional `building` on `GET /alerts` | `app/api/frontend_api.py` |
| Incidents | Optional `building` on `GET /incidents` | `app/main.py`, `app/domain/incidents/incident_engine.py` |
| Freshness | `building` accepted on `/api/v1/freshness`, `/api/v1/sources/liveness`, `/api/v1/data-gaps` next to the older `building_code` (must match if both are given) | `app/api/freshness_api.py` |
| Filter options | `GET /dashboard/filters` returns `building_code` for each building (new key) | `app/repositories/dashboard_core_pg.py` |
| UI | "All buildings" selector; alert and incident loads honour it | `app/dashboard/static/dashboard.html` |
| Unknown code | HTTP 400 `Unknown building_code: …` on every endpoint above | all of the above |

Earlier G4 commits: migration 013 (`building_code` NOT NULL + unique), seed building codes (`53a23f5`), second-building example SQL, ingestion isolation test.

## 2. Hardcoded dependencies found (grep of `app/`, `scripts/`, `deploy/`; not exhaustive)

| Finding | Blocks a new building? | Decision |
|---|---|---|
| No building names or asset codes in `app/` | No | — |
| `scripts/apply_migrations.py` maps the two seed building NAMES to `MAIN-TOWER` / `DATA-CENTER-ANNEX` for migration 013 | Only for buildings without a code | Kept. Any other building stops the migration with the `register-building` remedy |
| Time zone and work hours are per building (`buildings.timezone`, `work_*`) with fallback constants when NULL (`app/domain/alerts/alert_config.py`, `app/services/freshness.py`) | No | G1 |
| Five fixed system types (`CHECK` in `db/init/anzma_schema_and_seed.sql`, `app/config/reference_data.py`, `SYSTEM_CODES` in `frontend_api.py`) | Yes for a new system type | Documented; product decision |
| Fault/recovery pairs and fault lists (`FAULT_RECOVERY_PAIRS` in `dashboard_core_pg.py`, `FAULT_EVENTS` / `NOT_COUNTED_EVENTS` in `frontend_api.py`) | Yes for event types outside the catalogue (also rejected by ingestion as `UNKNOWN_EVENT_TYPE`) | Documented; extend catalogue + these lists together |
| Alert thresholds and rules are global (`alert_config.py`: `T`, `RULES`), with `Server Room` special cases (`alert_engine.py`, `chat_lite.py`) | No, but thresholds are not per site | Documented (KI-019) |
| `MAX_HOURS = 168` dashboard window cap | No | — |
| Chat assistant has no building parameter | Not a blocker; answers are not building-scoped | KI-022 |

## 3. Verification steps (run on a FRESH disposable project; never on `anzma-insight`, `anzma-d3-verify` or `anzma-acceptance-*`)

Rules from `docs/INCIDENTS.md` apply: every database command runs inside the disposable project's container; never export `PGHOST`/`PGPORT` pointing at localhost; host ports only from 18000–19999.

1. Prepare an environment file with safe ports and a token (the example file defaults to 5432/8000/11434, which collide with the live stack):

```bash
PROJECT=anzma-g4-proof-20261123
COMPOSE_FILE=deploy/compose/docker-compose.yml
cp deploy/examples/.env.example /tmp/g4.env
sed -i.bak -e 's/^DB_HOST_PORT=.*/DB_HOST_PORT=19432/' -e 's/^API_HOST_PORT=.*/API_HOST_PORT=19080/' \
           -e 's/^OLLAMA_HOST_PORT=.*/OLLAMA_HOST_PORT=19434/' -e "s/^ANZMA_API_TOKEN=.*/ANZMA_API_TOKEN=g4-$(openssl rand -hex 8)/" /tmp/g4.env
grep -E '^(DB|API|OLLAMA)_HOST_PORT|^ANZMA_API_TOKEN' /tmp/g4.env
```

2. Start it with NEW empty volumes and confirm the target before touching the database:

```bash
docker compose --env-file /tmp/g4.env -p "$PROJECT" -f "$COMPOSE_FILE" up -d --build
docker compose --env-file /tmp/g4.env -p "$PROJECT" -f "$COMPOSE_FILE" ps
docker compose --env-file /tmp/g4.env -p "$PROJECT" -f "$COMPOSE_FILE" exec -T api python scripts/apply_migrations.py
```

   The runner must report only the disposable database and skip what is already applied. If the project name or ports are not the ones above, stop.

3. No-database safety tests plus the full unit suite:

```bash
docker compose --env-file /tmp/g4.env -p "$PROJECT" -f "$COMPOSE_FILE" exec -T api python -m unittest tests.unit.test_g4_safety -v
docker compose --env-file /tmp/g4.env -p "$PROJECT" -f "$COMPOSE_FILE" exec -T api python -m unittest discover -s tests/unit
```

4. The public-API isolation proof (one run per fresh database):

```bash
docker compose --env-file /tmp/g4.env -p "$PROJECT" -f "$COMPOSE_FILE" exec -T \
  -e ANZMA_G4_DISPOSABLE_PROJECT="$PROJECT" api python -m unittest tests.integration.test_multibuilding_g4 -v
```

   It checks: unfiltered public responses equal the pre-change hashes; `?building=MAIN-TOWER` is identical before and after building B is ingested; B appears only under `?building=<B>` and in the unfiltered totals; a key for B cannot write A's assets (`ASSET_NOT_PERMITTED`); unknown codes return 400; the selector exists in `/dashboard`.

5. Full regression on the same stack, one table versus the last baseline recorded in `docs/PHASE1_STATUS.md` (the unit suite gains the 3 new tests). Run each suite exactly as recorded there; any difference must be explained.

6. Pre-012 safety (database without the asset-state tables): covered by `tests/unit/test_g4_safety.py` (cursor double, no database). A live check is optional: restore the H0 dump into a second disposable project, apply only migrations up to 011, call `POST /alerts/{id}/explain`, expect HTTP 200.

7. Clean up only your own project:

```bash
docker compose --env-file /tmp/g4.env -p "$PROJECT" -f "$COMPOSE_FILE" down -v
```

## 4. Acceptance for G4

* Step 3 and 4 pass; step 5 shows no regression; step 6 unit tests pass.
* Amr reviews the `dashboard.html`, `main.py` and `/dashboard/filters` touches (`docs/CHANGE_COORDINATION.md`).
* Then update `docs/PHASE1_STATUS.md` (G4 DONE with the command output) and push after review.

## 5. Not covered by G4

Per-site thresholds, new system types, chat scoping (KI-022), and a database-level seed of a third building in the default compose install.
