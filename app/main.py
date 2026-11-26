import os
import re
import json
from typing import Any
from datetime import datetime, timedelta, timezone
from uuid import UUID


import asyncio
import logging

import psycopg2
import psycopg2.extras
import socketio
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from psycopg import OperationalError

from app.repositories.dashboard_core_pg import (
    DashboardError,
    NoDataError,
    get_dashboard_summary,
    get_filter_options,
    open_readonly,
)

load_dotenv()

from app.adapters.database import db_adapter as da
from app.adapters.model_providers.ollama_provider import OllamaProvider
from app.adapters.model_providers import ai_explainer
from app.domain.alerts import alert_config as cfg
from app.domain.evidence import evidence as evd
from app.domain.incidents import incident_engine as inc
from app.services import pipeline
from app.services import ingestion
from app.services import rate_limit
from app.api import connector_setup_api
from app.security import (authenticator, configure_cors, cors_origins_from_env, socket_authenticate,
                          socketio_cors_origins, token_from_authorization)


# Every route below is registered on `api`; `asgi_app` (bottom of the file) wraps it with Socket.IO for live alerts.
api = FastAPI(title="Anzma Dashboard API")
CORS_ALLOWED_ORIGINS = cors_origins_from_env()
configure_cors(api, CORS_ALLOWED_ORIGINS)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "dashboard", "static")
logger = logging.getLogger("anzma")
model_provider = OllamaProvider.from_env()
AUTH_DISABLED = os.getenv("ANZMA_AUTH_DISABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


@api.middleware("http")
async def authenticate_user_api(request: Request, call_next):
    """Protect every operational REST route; keep liveness, UI assets and ingestion auth separate."""
    if AUTH_DISABLED:
        return await call_next(request)
    path = request.url.path
    public = path in {"/", "/dashboard", "/health", "/ready", "/auth/session", "/auth/logout",
                      "/docs", "/redoc", "/openapi.json"} or path.startswith("/static/")
    is_ingestion = path.startswith("/api/v1/ingest/")
    if not public and not is_ingestion:
        authorization = request.headers.get("authorization")
        token = token_from_authorization(authorization) or request.cookies.get("anzma_session")
        if not authorization and not token:
            return JSONResponse(status_code=401, content={"detail": {"code": "AUTH_MISSING",
                "message": "Bearer credentials are required."}}, headers={"WWW-Authenticate": "Bearer"})
        if not authenticator.authenticate(token):
            return JSONResponse(status_code=401, content={"detail": {"code": "AUTH_INVALID",
                "message": "Bearer credentials are invalid."}}, headers={"WWW-Authenticate": "Bearer"})
    return await call_next(request)

# a lost / unreachable database from either driver, or from the Day 2 adapter, is a 503, never a crash
DB_ERRORS = (OperationalError, psycopg2.OperationalError, da.DatabaseUnavailable)

api.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@api.get("/", include_in_schema=False, dependencies=[])
@api.get("/dashboard", include_in_schema=False, dependencies=[])
def dashboard():
    return FileResponse(os.path.join(STATIC_DIR, "dashboard.html"))


@api.get("/setup", include_in_schema=False)
def connector_setup_page():
    return FileResponse(os.path.join(STATIC_DIR, "setup.html"))


def day2_connect(**options):
    """Open a psycopg2 connection for Ahmed's Day 2 engine (options, e.g. connect_timeout, go to psycopg2)."""
    return psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "anzma"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", ""),
        cursor_factory=psycopg2.extras.RealDictCursor,
        **options,
    )


@api.get("/health", dependencies=[])
def health():
    """Liveness only: the process is up. It does not look at the database."""
    return {"status": "ok"}


@api.get("/ready", dependencies=[])
def ready():
    """Readiness: 200 only while the database answers a query, otherwise 503 (no connection details in the reply)."""
    try:
        conn = day2_connect(connect_timeout=3)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            conn.close()
    except DB_ERRORS as exc:
        logger.warning("readiness check failed: %s", str(exc).strip()[:200])
        return JSONResponse(status_code=503, content={"status": "not_ready", "database": "down"})
    return {"status": "ready", "database": "up"}


@api.post("/auth/session", dependencies=[])
def create_user_session(request: Request, authorization: str | None = Header(default=None)):
    """Exchange an operator-provided bearer secret for a short-lived HttpOnly browser cookie."""
    if not authorization:
        return JSONResponse(status_code=401, content={"detail": {"code": "AUTH_MISSING",
            "message": "Bearer credentials are required."}}, headers={"WWW-Authenticate": "Bearer"})
    token = token_from_authorization(authorization)
    if not authenticator.authenticate(token):
        return JSONResponse(status_code=401, content={"detail": {"code": "AUTH_INVALID",
            "message": "Bearer credentials are invalid."}}, headers={"WWW-Authenticate": "Bearer"})
    response = JSONResponse({"status": "authenticated"})
    response.set_cookie("anzma_session", token, max_age=8 * 60 * 60, httponly=True,
                        secure=os.getenv("ANZMA_COOKIE_SECURE", "false").lower() == "true",
                        samesite="strict", path="/")
    return response


@api.post("/auth/logout", dependencies=[])
def delete_user_session():
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie("anzma_session", path="/", httponly=True, samesite="strict")
    return response


@api.get("/dashboard/filters")
def dashboard_filters():
    try:
        with open_readonly() as conn:
            return get_filter_options(conn)
    except (NoDataError, *DB_ERRORS) as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@api.get("/dashboard/summary")
def dashboard_summary(
    system_type: str | None = Query(default=None),
    building_id: int | None = Query(default=None),
    start_time: str | None = Query(default=None),
    end_time: str | None = Query(default=None),
):
    try:
        with open_readonly() as conn:
            return get_dashboard_summary(
                conn,
                system_type=system_type,
                building_id=building_id,
                start_time=start_time,
                end_time=end_time,
            )
    except (DashboardError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except NoDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except DB_ERRORS as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@api.get("/incidents")
def list_incidents(building: str | None = Query(default=None)):
    try:
        with day2_connect() as conn:
            with conn.cursor() as cur:
                if building not in (None, ""):
                    cur.execute("SELECT 1 FROM buildings WHERE building_code=%s", (building,))
                    if cur.fetchone() is None:
                        raise HTTPException(status_code=400, detail=f"Unknown building_code: {building}.")
                return {"incidents": inc.list_incidents(cur, building_code=building)}
    except HTTPException:
        raise
    except DB_ERRORS as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@api.get("/incidents/{incident_id}")
def get_incident(incident_id: int):
    try:
        with day2_connect() as conn:
            with conn.cursor() as cur:
                incident = inc.get_incident(cur, incident_id)
                if incident is None:
                    raise HTTPException(status_code=404, detail="Incident not found")
                return incident
    except HTTPException:
        raise
    except DB_ERRORS as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@api.get("/incidents/{incident_id}/evidence")
def get_incident_evidence(incident_id: int):
    try:
        with day2_connect() as conn:
            with conn.cursor() as cur:
                incident = inc.get_incident(cur, incident_id)
                if incident is None:
                    raise HTTPException(status_code=404, detail="Incident not found")
                return evd.incident_evidence(cur, incident_id)
    except HTTPException:
        raise
    except DB_ERRORS as exc:
        raise HTTPException(status_code=503, detail=str(exc))


class IncidentStatusIn(BaseModel):
    status: str


@api.post("/incidents/{incident_id}/status", tags=["Incidents"])
def set_incident_status(incident_id: int, body: IncidentStatusIn):
    """Move an incident along open -> acknowledged -> resolved (resolved is final); its alerts follow."""
    try:
        with day2_connect() as conn:
            with conn.cursor() as cur:
                inc.set_status(cur, incident_id, body.status)
                conn.commit()
                return inc.get_incident(cur, incident_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except DB_ERRORS as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@api.post("/alerts/{alert_id}/explain")
def explain_alert(alert_id: int, use_llm: bool = Query(default=False)):
    try:
        with day2_connect() as conn:
            with conn.cursor() as cur:
                alert = da.get_alert(cur, alert_id)
                if alert is None:
                    raise HTTPException(status_code=404, detail="Alert not found")

                result = pipeline.explain_alert(cur, alert_id, use_llm=use_llm, provider=model_provider)
                conn.commit()
                return result
    except HTTPException:
        raise
    except DB_ERRORS as exc:
        raise HTTPException(status_code=503, detail=str(exc))


class AskRequest(BaseModel):
    question: str
    use_llm: bool = False


@api.post("/ask")
def ask(request: AskRequest) -> dict[str, Any]:
    """
    Minimal Day 2 operational-question router.

    Routes a CRAC / temperature question to the HVAC temperature evidence,
    explanation, validation, and recommendation workflow.
    """
    question = request.question.lower()

    if not any(word in question for word in ("crac", "temperature", "hvac", "cooling")):
        raise HTTPException(
            status_code=501,
            detail=(
                "This Day 2 demo supports CRAC, HVAC, temperature, and cooling "
                "questions. Other domains will be added in Day 3."
            ),
        )

    try:
        with day2_connect() as conn:
            with conn.cursor() as cur:
                pipeline.detect(cur)
                conn.commit()

                cur.execute(
                    """
                    SELECT alert_id
                    FROM alerts
                    WHERE rule_id = 'HVAC_HIGH_TEMP'
                    ORDER BY detected_ts DESC, alert_id DESC
                    LIMIT 1
                    """
                )
                row = cur.fetchone()

                if row is None:
                    raise HTTPException(
                        status_code=404,
                        detail="No HVAC temperature alert is currently available.",
                    )

                alert_id = row["alert_id"]
                result = pipeline.explain_alert(
                    cur,
                    alert_id,
                    use_llm=request.use_llm,
                    provider=model_provider,
                )
                conn.commit()

                return {
                    "question": request.question,
                    "selected_tool": "hvac_temperature_evidence",
                    "alert_id": alert_id,
                    **result,
                }
    except HTTPException:
        raise
    except DB_ERRORS as exc:
        raise HTTPException(status_code=503, detail=str(exc))

# ======================================================================================================
# Dashboard page, assistant widget, detection and live alerts
# ======================================================================================================
from app.api import alert_stream_api, analytics_api, chat_lite, frontend_api, freshness_api  # noqa: E402

api.include_router(frontend_api.create_router())
api.include_router(connector_setup_api.create_router())
api.include_router(freshness_api.create_router())
api.include_router(alert_stream_api.create_router())
api.include_router(analytics_api.create_router(day2_connect, db_errors=DB_ERRORS))
api.include_router(chat_lite.create_router(
    day2_connect, pipeline, inc, use_llm_default=os.getenv("CHAT_USE_LLM", "true").lower() == "true",
    db_errors=DB_ERRORS, model_provider=model_provider))


class IngestionConnector(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=32)


class IngestionSource(BaseModel):
    model_config = ConfigDict(extra="allow")
    source_code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    connector: IngestionConnector
    connector_status: dict[str, Any] | None = None


class IngestionBatch(BaseModel):
    model_config = ConfigDict(extra="allow")
    contract_version: str
    batch_id: str
    sent_at: str
    source: IngestionSource
    building_code: str
    records: list[Any]
    delivery_mode: str = "LIVE"

    @field_validator("contract_version")
    @classmethod
    def _contract_version(cls, value):
        if not re.fullmatch(r"\d+\.\d+", value):
            raise ValueError("contract_version must use MAJOR.MINOR format")
        return value

    @field_validator("batch_id")
    @classmethod
    def _batch_id(cls, value):
        try:
            return str(UUID(value))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("batch_id must be a UUID") from exc

    @field_validator("sent_at")
    @classmethod
    def _sent_at_format(cls, value):
        if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z", value):
            raise ValueError("sent_at must be RFC 3339 UTC with a trailing Z")
        return value

    @field_validator("records")
    @classmethod
    def _record_count(cls, value):
        return value


def _ingestion_schema_ready(cur):
    cur.execute("""SELECT to_regclass('public.data_sources') AS sources,
                          to_regclass('public.v_unmapped_external_assets') AS unmapped""")
    row = cur.fetchone()
    return row["sources"] is not None and row["unmapped"] is not None


def _ingestion_error_response(exc: ingestion.IngestionError, headers: dict[str, str] | None = None):
    return JSONResponse(status_code=exc.status, content=exc.response(exc.batch_id), headers=headers)


@api.exception_handler(RequestValidationError)
async def ingestion_request_validation_error(request: Request, exc: RequestValidationError):
    if not request.url.path.startswith("/api/v1/ingest/"):
        return await request_validation_exception_handler(request, exc)

    authorization = request.headers.get("authorization")
    if authorization is None:
        return _ingestion_error_response(ingestion.IngestionError(
            401, "AUTH_MISSING", "A source API key is required."))
    token = token_from_authorization(authorization)
    if ingestion.parse_key_prefix(token) is None:
        return _ingestion_error_response(ingestion.IngestionError(
            401, "AUTH_INVALID", "A valid source API key is required."))

    errors = exc.errors()
    location = next((error.get("loc", ()) for error in errors if error.get("loc")), ())
    field_path = ".".join(str(part) for part in location
                           if part not in {"body", "query", "path", "header", "cookie"}) or "request"
    return _ingestion_error_response(ingestion.IngestionError(
        422, "SCHEMA_INVALID", f"Request validation failed at field '{field_path}'."))


@api.post("/api/v1/ingest/batches", tags=["Ingestion"], dependencies=[])
def ingest_batch(body: IngestionBatch, request: Request,
                 authorization: str | None = Header(default=None)):
    """Ingest a canonical batch. Existing POST /events and POST /alerts remain unchanged."""
    batch_id = body.batch_id
    try:
        content_length = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": {"code": "INVALID_CONTENT_LENGTH",
                                                                   "message": "Content-Length must be an integer.",
                                                                   "retryable": False}})
    if content_length > 5 * 1024 * 1024:
        return JSONResponse(status_code=413, content=ingestion.IngestionError(
            413, "BATCH_TOO_LARGE", "Request body exceeds 5 MiB.", batch_id=batch_id).response())
    body_size = len(json.dumps(body.model_dump(mode="json"), ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8"))
    if body_size > 5 * 1024 * 1024:
        return JSONResponse(status_code=413, content=ingestion.IngestionError(
            413, "BATCH_TOO_LARGE", "Request body exceeds 5 MiB.", batch_id=batch_id).response())
    try:
        conn = day2_connect()
        try:
            with conn.cursor() as cur:
                if not _ingestion_schema_ready(cur):
                    raise ingestion.IngestionError(503, "INGESTION_SCHEMA_NOT_APPLIED",
                                                   "Apply database migrations 006–008 before using Core ingestion.",
                                                   retryable=False, batch_id=batch_id)
                source = ingestion.authenticate(cur, authorization)
                conn.commit()  # preserve successful credential use independently of later request admission
                key_prefix = ingestion.parse_key_prefix((authorization or "")[7:].strip())
                retry_after = rate_limit.ingestion_rate_limiter.retry_after(key_prefix or "")
                if retry_after is not None:
                    limited = ingestion.IngestionError(
                        429, "RATE_LIMITED", "Ingestion rate limit exceeded; retry after the indicated delay.",
                        retryable=True)
                    return _ingestion_error_response(limited, headers={"Retry-After": str(retry_after)})
                try:
                    result, replay = ingestion.process_batch(
                        cur, source, body.model_dump(mode="json"), datetime.now(timezone.utc))
                except ingestion.IngestionError:
                    conn.commit()  # retain last_used_at for a valid credential; no fact rows are written on envelope errors
                    raise
                conn.commit()
                headers = {"Idempotent-Replay": "true"} if replay else {}
                status = 422 if result["status"] == "REJECTED" else 200
                return JSONResponse(status_code=status, content=jsonable_encoder(result), headers=headers)
        except ingestion.IngestionError as exc:
            return _ingestion_error_response(exc)
        finally:
            conn.close()
    except DB_ERRORS as exc:
        logger.warning("ingestion database unavailable: %s", str(exc).strip()[:200])
        return JSONResponse(status_code=503, content=ingestion.IngestionError(
            503, "SERVICE_UNAVAILABLE", "Database is unavailable; retry the same batch_id.",
            retryable=True, batch_id=batch_id).response())


@api.get("/api/v1/ingest/batches/{batch_id}", tags=["Ingestion"], dependencies=[])
def get_ingestion_batch(batch_id: str, authorization: str | None = Header(default=None)):
    """Return the recorded per-record outcomes and complete rejection payloads for this source's batch."""
    try:
        try:
                batch_uuid = str(UUID(batch_id))
        except (ValueError, TypeError, AttributeError):
            raise ingestion.IngestionError(422, "SCHEMA_INVALID", "batch_id must be a UUID.", batch_id=batch_id)
        with day2_connect() as conn:
            with conn.cursor() as cur:
                if not _ingestion_schema_ready(cur):
                    raise ingestion.IngestionError(503, "INGESTION_SCHEMA_NOT_APPLIED",
                                                   "Apply database migrations 006–008 before using Core ingestion.",
                                                   retryable=False, batch_id=batch_id)
                source = ingestion.authenticate(cur, authorization)
                cur.execute("""SELECT response_payload FROM ingestion_batches
                               WHERE source_id=%s AND batch_id=%s""", (source["source_id"], batch_uuid))
                row = cur.fetchone()
                if not row:
                    raise ingestion.IngestionError(404, "BATCH_NOT_FOUND", "Batch was not found for this source.",
                                                   batch_id=str(batch_uuid))
                result = dict(row["response_payload"])
                cur.execute("""SELECT record_index,record_type,reason_code,message,payload,external_id
                               FROM ingestion_rejections WHERE ingestion_batch_id=
                               (SELECT ingestion_batch_id FROM ingestion_batches WHERE source_id=%s AND batch_id=%s)
                               ORDER BY record_index""", (source["source_id"], batch_uuid))
                result["rejected"] = [{"index": item["record_index"], "record_type": item["record_type"],
                                        "reason_code": item["reason_code"], "message": item["message"],
                                        "payload": item["payload"], "external_id": item["external_id"]}
                                       for item in cur.fetchall()]
                result["rejected_truncated"] = False
                conn.commit()
                return result
    except ingestion.IngestionError as exc:
        return _ingestion_error_response(exc)
    except DB_ERRORS as exc:
        logger.warning("ingestion batch lookup unavailable: %s", str(exc).strip()[:200])
        return JSONResponse(status_code=503, content=ingestion.IngestionError(
            503, "SERVICE_UNAVAILABLE", "Database is unavailable.", retryable=True, batch_id=batch_id).response())


@api.post("/alerts/run", tags=["Alerts"])
def run_detection(hours: int | None = Query(default=None, ge=1, le=720)):
    """Run the alert rules over the stored data and group the alerts into incidents. Safe to repeat: alerts that
    already exist are not duplicated. The live dashboard receives any new alert through Socket.IO."""
    try:
        with day2_connect() as conn:
            with conn.cursor() as cur:
                result = pipeline.detect(cur, hours=hours)
                conn.commit()
                return jsonable_encoder(result)
    except DB_ERRORS as exc:
        raise HTTPException(status_code=503, detail=str(exc))

SEVERITIES = ("INFO", "WARNING", "CRITICAL")
FUTURE_TOLERANCE = timedelta(minutes=5)          # same rule as API_CONTRACT.md: now + 5 min at most


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone, e.g. 2026-11-11T08:00:00Z")
    return value.astimezone(timezone.utc)


class AlertCreate(BaseModel):
    rule_id: str                                         # must be a key of alert_config.RULES
    asset_id: int
    severity: str                                        # INFO | WARNING | CRITICAL (any case)
    summary: str = Field(min_length=1, max_length=2000)
    title: str | None = Field(default=None, max_length=200)   # default: "<rule title> - <asset name>"
    first_ts: datetime
    last_ts: datetime | None = None                      # default: first_ts
    facts: dict[str, Any] = Field(default_factory=dict)  # optional measured facts, kept in alerts.evidence

    @field_validator("severity")
    @classmethod
    def _severity(cls, v):
        v = v.upper()
        if v not in SEVERITIES:
            raise ValueError("severity must be INFO, WARNING or CRITICAL")
        return v

    @model_validator(mode="after")
    def _times(self):
        self.first_ts = _utc(self.first_ts)
        self.last_ts = _utc(self.last_ts) if self.last_ts else self.first_ts
        if self.last_ts < self.first_ts:
            raise ValueError("last_ts must not be before first_ts")
        if self.last_ts > datetime.now(timezone.utc) + FUTURE_TOLERANCE:
            raise ValueError("timestamps in the future are rejected")
        return self


@api.post("/alerts", status_code=201, tags=["Alerts"])
def create_alert(body: AlertCreate):
    """Store one alert by hand (same table, same shape as the rule engine's alerts) and group it into an incident."""
    rule = cfg.RULES.get(body.rule_id)
    if rule is None:
        raise HTTPException(422, f"Unknown rule_id. Allowed: {', '.join(sorted(cfg.RULES))}.")
    try:
        with day2_connect() as conn:
            with conn.cursor() as cur:
                asset = da.get_asset(cur, body.asset_id)
                if asset is None:
                    raise HTTPException(422, f"Unknown asset_id {body.asset_id}.")
                new_ids = da.store_alerts(cur, [{
                    "rule_id": body.rule_id,
                    "detection_method": rule["method"],              # taken from the rule, never from the client
                    "severity": body.severity,
                    "asset_id": body.asset_id,
                    "title": body.title or f"{rule['title']} - {asset['name']}",
                    "summary": body.summary,
                    "first_ts": body.first_ts,
                    "last_ts": body.last_ts,
                    "detected_ts": datetime.now(timezone.utc),
                    "evidence": {"thresholds": {}, "facts": body.facts, "event_count": 0, "events": [],
                                 "source": "manual"},
                }])
                if not new_ids:                                        # store_alerts skips duplicates silently
                    raise HTTPException(409, "An alert with the same rule_id, asset_id and first_ts already exists.")
                inc.assign_incidents(cur)                              # same grouping the detector uses
                conn.commit()
                return jsonable_encoder(da.get_alert(cur, new_ids[0]))
    except HTTPException:
        raise
    except DB_ERRORS as exc:
        raise HTTPException(503, str(exc))


class EventCreate(BaseModel):
    asset_id: int
    event_type: str                                      # event_types.name, e.g. "door_forced" (any case)
    ts: datetime
    severity: str = "INFO"
    details: dict[str, Any] | None = None

    @field_validator("severity")
    @classmethod
    def _severity(cls, v):
        v = v.upper()
        if v not in SEVERITIES:
            raise ValueError("severity must be INFO, WARNING or CRITICAL")
        return v

    @field_validator("ts")
    @classmethod
    def _ts(cls, v):
        v = _utc(v)
        if v > datetime.now(timezone.utc) + FUTURE_TOLERANCE:
            raise ValueError("timestamps in the future are rejected")
        return v


@api.post("/events", status_code=201, tags=["Events"])
def create_event(body: EventCreate):
    """Store one event in event_records. The next detection run (automatic every DETECT_INTERVAL s) can raise alerts from it."""
    name = body.event_type.lower()
    try:
        with day2_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT a.asset_id, t.event_type_id,
                                      EXISTS (SELECT 1 FROM event_type_system_compat c
                                              WHERE c.event_type_id = t.event_type_id
                                                AND c.system_type_id = a.system_type_id) AS allowed
                               FROM assets a LEFT JOIN event_types t ON t.name = %s
                               WHERE a.asset_id = %s""", (name, body.asset_id))
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(422, f"Unknown asset_id {body.asset_id}.")
                if row["event_type_id"] is None:
                    raise HTTPException(422, f"Unknown event_type '{name}'.")
                if not row["allowed"]:
                    raise HTTPException(422, f"event_type '{name}' is not allowed for this asset's system.")
                cur.execute("SELECT to_regclass('public.uq_event_legacy_natural_key') IS NOT NULL AS partial_key")
                has_partial_key = cur.fetchone()["partial_key"]
                conflict_target = ("ON CONFLICT (asset_id, event_type_id, ts) WHERE source_event_id IS NULL DO NOTHING"
                                   if has_partial_key else
                                   "ON CONFLICT (asset_id, event_type_id, ts) DO NOTHING")
                cur.execute(f"""INSERT INTO event_records (asset_id, event_type_id, ts, severity, details)
                               VALUES (%s,%s,%s,%s,%s)
                               {conflict_target}
                               RETURNING event_id""",
                            (body.asset_id, row["event_type_id"], body.ts, body.severity.lower(),
                             da.Json(cur, body.details) if body.details is not None else None))
                created = cur.fetchone()
                if created is None:
                    raise HTTPException(409, "This event already exists (same asset, event_type and ts).")
                conn.commit()
                return {"id": created["event_id"], "asset_id": body.asset_id, "event_type": name,
                        "ts": body.ts.isoformat().replace("+00:00", "Z"), "severity": body.severity}
    except HTTPException:
        raise
    except DB_ERRORS as exc:
        raise HTTPException(503, str(exc))
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins=socketio_cors_origins(CORS_ALLOWED_ORIGINS))


@sio.event
async def connect(sid, environ, auth=None):
    """Socket.IO shares the user API bearer token, separately from ingestion keys."""
    if AUTH_DISABLED:
        return True
    if not socket_authenticate(auth, environ.get("HTTP_COOKIE")):
        return False
    return True


DETECT_INTERVAL = int(os.getenv("DETECT_INTERVAL", "60"))        # seconds between automatic detection runs
POLL_INTERVAL = int(os.getenv("ALERT_POLL_INTERVAL", "3"))       # seconds between checks for new alerts
_background = []


def _detect_once():
    with day2_connect() as conn:
        with conn.cursor() as cur:
            pipeline.detect(cur)
            conn.commit()


def _new_alerts(last_id):
    with open_readonly() as conn:
        return frontend_api.alerts_after(conn, last_id)


def _latest_alert_id():
    with open_readonly() as conn:
        return frontend_api.latest_alert_id(conn)


async def _alert_loop():
    """Detect periodically and push every newly stored alert to the connected pages ("new_alert")."""
    last_id, since_detect = None, DETECT_INTERVAL
    while True:
        try:
            if last_id is None:
                last_id = await asyncio.to_thread(_latest_alert_id)
            if since_detect >= DETECT_INTERVAL:
                await asyncio.to_thread(_detect_once)
                since_detect = 0
            for alert in await asyncio.to_thread(_new_alerts, last_id):
                await sio.emit("new_alert", jsonable_encoder(alert))
                last_id = max(last_id, alert["id"])
        except Exception as exc:                  # keep the loop alive while the database is down or starting
            logger.warning("alert loop: %s", exc)
        await asyncio.sleep(POLL_INTERVAL)
        since_detect += POLL_INTERVAL


async def _startup():
    if not AUTH_DISABLED and len(os.getenv("ANZMA_API_TOKEN", "")) < 32:
        raise RuntimeError("ANZMA_API_TOKEN must contain at least 32 characters.")
    _background.append(asyncio.create_task(_alert_loop()))


async def _shutdown():
    for task in _background:
        task.cancel()


asgi_app = socketio.ASGIApp(sio, other_asgi_app=api, on_startup=_startup, on_shutdown=_shutdown)
