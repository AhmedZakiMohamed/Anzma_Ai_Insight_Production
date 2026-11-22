"""
test_frontend_api.py - the API the dashboard page calls, tested on a REAL PostgreSQL built from the files Docker uses:
db/init/anzma_schema_and_seed.sql, then db/migrations/003 incidents, 004 views, 005 alert uniqueness and 006 ingestion.

Run:  PGHOST=localhost PGUSER=postgres PGPASSWORD=... python test_frontend_api.py      (user may CREATE DATABASE)
"""
import os
import sys
from datetime import timedelta
from datetime import datetime, timezone
import hashlib
import json
import re
import secrets
import subprocess
import uuid

TEST_DB = "anzma_api_test"
HOST, PORT = os.getenv("PGHOST", "localhost"), os.getenv("PGPORT", "5432")
USER, PASSWORD = os.getenv("PGUSER", "postgres"), os.getenv("PGPASSWORD", "")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import psycopg                                    # noqa: E402
from psycopg.rows import dict_row                 # noqa: E402


def sql_file(path):
    return open(os.path.join(ROOT, path), encoding="utf-8").read()


with psycopg.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, dbname="postgres", autocommit=True) as admin:
    admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {TEST_DB}")
DB = psycopg.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, dbname=TEST_DB, row_factory=dict_row,
                     autocommit=True)
DB.execute(sql_file("db/init/anzma_schema_and_seed.sql"))
for _m in ("003_incidents", "004_chat_views", "005_alert_uniqueness", "006_ingestion_foundation",
           "007_event_source_identity", "008_ingestion_contract_conformance", "009_freshness_expectations",
           "010_data_gap_lifecycle", "011_source_liveness"):   # runtime sequence
    DB.execute(sql_file(f"db/migrations/{_m}.sql"))

TEST_USER_TOKEN = "integration-test-user-api-token-not-for-deployment"
os.environ.update(PGHOST=HOST, PGPORT=PORT, PGUSER=USER, PGPASSWORD=PASSWORD, PGDATABASE=TEST_DB,
                  ANZMA_API_TOKEN=TEST_USER_TOKEN,
                  ANZMA_INGEST_RATE_LIMIT_PER_MIN="100000", ANZMA_INGEST_RATE_LIMIT_BURST="100000")
from fastapi.testclient import TestClient         # noqa: E402
import app.main as App                                        # noqa: E402
from app.services.rate_limit import InMemoryTokenBucket, RateLimitSettings  # noqa: E402

class UserAuthenticatedTestClient(TestClient):
    def request(self, method, url, *args, **kwargs):
        path = str(url).split("?", 1)[0]
        if not path.startswith("/api/v1/ingest/"):
            headers = dict(kwargs.pop("headers", {}) or {})
            headers.setdefault("Authorization", f"Bearer {TEST_USER_TOKEN}")
            kwargs["headers"] = headers
        return super().request(method, url, *args, **kwargs)


client = UserAuthenticatedTestClient(App.api)
RESULTS = []


def test(name):
    def deco(fn):
        RESULTS.append((name, fn))
        return fn
    return deco


def one(sql, *params):
    return DB.execute(sql, params).fetchone()


@test("user API auth: missing, invalid, and valid credentials; health remains public")
def _():
    anonymous = TestClient(App.api)
    assert App.authenticator.authenticate("short") is False
    missing = anonymous.get("/incidents")
    invalid = anonymous.get("/incidents", headers={"Authorization": "Bearer invalid"})
    assert missing.status_code == 401 and missing.json()["detail"]["code"] == "AUTH_MISSING"
    assert invalid.status_code == 401 and invalid.json()["detail"]["code"] == "AUTH_INVALID"
    old_token = os.environ.pop("ANZMA_API_TOKEN")
    try:
        import asyncio
        try:
            asyncio.run(App._startup())
            raise AssertionError("startup accepted a missing API token")
        except RuntimeError as exc:
            assert "at least 32 characters" in str(exc)
    finally:
        os.environ["ANZMA_API_TOKEN"] = old_token
    assert anonymous.get("/health").status_code == 200
    assert anonymous.get("/ready").status_code == 200
    assert anonymous.get("/dashboard").status_code == 200
    assert anonymous.get("/docs").status_code == 200
    assert anonymous.get("/openapi.json").status_code == 200
    assert client.get("/incidents").status_code == 200
    session = anonymous.post("/auth/session", headers={"Authorization": f"Bearer {TEST_USER_TOKEN}"})
    assert session.status_code == 200 and "httponly" in session.headers["set-cookie"].lower()
    assert anonymous.get("/incidents").status_code == 200
    assert anonymous.post("/auth/logout").status_code == 200
    assert anonymous.get("/incidents").status_code == 401
    # Socket authentication is the same replaceable user authenticator, independent of ingestion keys.
    assert asyncio.run(App.connect("unit", {}, {"token": TEST_USER_TOKEN})) is True
    assert asyncio.run(App.connect("unit", {"HTTP_COOKIE": f"anzma_session={TEST_USER_TOKEN}"}, None)) is True
    assert asyncio.run(App.connect("unit", {}, {"token": "invalid"})) is False
    assert asyncio.run(App.connect("unit", {}, None)) is False


def _ingestion_setup():
    asset = one("""SELECT a.asset_code, a.asset_id, a.system_type_id, l.building_id
                  FROM assets a JOIN locations l USING(location_id)
                  JOIN system_types st USING(system_type_id) WHERE st.code='HVAC' ORDER BY a.asset_id LIMIT 1""")
    metric = one("""SELECT m.name, m.unit FROM metric_types m JOIN metric_type_system_compat c USING(metric_id)
                  WHERE c.system_type_id=%s ORDER BY m.metric_id LIMIT 1""", asset["system_type_id"])
    building_code = "API-INGEST-TEST"
    DB.execute("UPDATE buildings SET building_code=%s WHERE building_id=%s", (building_code, asset["building_id"]))
    source_code = f"api-ingest-test-{uuid.uuid4().hex[:8]}"
    DB.execute("""INSERT INTO data_sources(source_code,building_id,connector_name,connector_version)
                   VALUES (%s,%s,'integration-test','1')""", (source_code, asset["building_id"]))
    token = f"azk_{secrets.token_hex(6)}_{secrets.token_urlsafe(32)}"
    DB.execute("""INSERT INTO source_credentials(source_id,key_prefix,key_hash)
                   SELECT source_id,%s,%s FROM data_sources WHERE source_code=%s""",
               (token.split("_")[1], hashlib.sha256(token.encode()).hexdigest(), source_code))
    return asset, metric, building_code, source_code, token


@test("v1 ingestion: auth, per-record partial commit, replay, audit retrieval and immutable conflict")
def _():
    asset, metric, building_code, source_code, token = _ingestion_setup()
    auth = {"Authorization": f"Bearer {token}"}
    observed_dt = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=5)
    observed_at = observed_dt.isoformat().replace("+00:00", "Z")
    good = {"record_type": "telemetry", "asset": {"asset_code": asset["asset_code"]},
            "observed_at": observed_at, "metric_code": metric["name"], "value": 12.5, "unit": metric["unit"]}
    bad = {**good, "value": 13.5, "unit": "wrong-unit"}
    batch_id = str(uuid.uuid4())
    body = {"contract_version": "1.0", "batch_id": batch_id,
            "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "source": {"source_code": source_code, "connector": {"name": "integration-test", "version": "1"}},
            "building_code": building_code, "records": [good, bad]}
    assert client.post("/api/v1/ingest/batches", json=body).status_code == 401
    response = client.post("/api/v1/ingest/batches", json=body, headers=auth)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "PARTIAL" and result["transaction"] == "COMMITTED"
    assert [item["outcome"] for item in result["results"]] == ["ACCEPTED", "REJECTED"]
    assert result["counts"] == {"received": 2, "accepted": 1, "duplicate": 0, "rejected": 1}
    prior_value = one("SELECT value FROM telemetry_records WHERE asset_id=%s AND ts=%s",
                      asset["asset_id"], observed_dt)["value"]
    assert prior_value == 12.5
    replay = client.post("/api/v1/ingest/batches", json=body, headers=auth)
    assert replay.status_code == 200 and replay.headers.get("Idempotent-Replay") == "true"
    assert replay.json() == result
    duplicate_body = {**body, "batch_id": str(uuid.uuid4()), "records": [good]}
    duplicate = client.post("/api/v1/ingest/batches", json=duplicate_body, headers=auth)
    assert duplicate.status_code == 200 and duplicate.json()["results"][0]["outcome"] == "DUPLICATE"
    reused = {**body, "records": [{**good, "value": 14.0}]}
    assert client.post("/api/v1/ingest/batches", json=reused, headers=auth).status_code == 409
    conflict_body = {**body, "batch_id": str(uuid.uuid4()), "records": [{**good, "value": 99.0}]}
    conflict = client.post("/api/v1/ingest/batches", json=conflict_body, headers=auth)
    assert conflict.status_code == 422 and conflict.json()["status"] == "REJECTED"
    assert one("SELECT value FROM telemetry_records WHERE asset_id=%s AND ts=%s",
               asset["asset_id"], observed_dt)["value"] == 12.5
    audit = client.get(f"/api/v1/ingest/batches/{batch_id}", headers=auth)
    assert audit.status_code == 200 and audit.json()["rejected"][0]["payload"] == bad
    assert one("SELECT count(*) AS n FROM ingestion_rejections")["n"] == 2

    event_dt = observed_dt + timedelta(seconds=30)
    event_time = event_dt.isoformat().replace("+00:00", "Z")
    event_a = {"record_type": "event", "asset": {"asset_code": asset["asset_code"]},
               "observed_at": event_time, "source_event_id": "source-event-a",
               "event_type": "hvac_fault", "severity": "INFO", "details": {"origin": "a"}}
    event_b = {**event_a, "source_event_id": "source-event-b", "details": {"origin": "b"}}
    event_batch = {**body, "batch_id": str(uuid.uuid4()), "records": [event_a, event_b]}
    event_result = client.post("/api/v1/ingest/batches", json=event_batch, headers=auth)
    assert event_result.status_code == 200
    assert [r["outcome"] for r in event_result.json()["results"]] == ["ACCEPTED", "ACCEPTED"]
    at_same_instant = DB.execute("""SELECT count(*) AS n FROM event_records
                                  WHERE source_id=(SELECT source_id FROM data_sources WHERE source_code=%s)
                                    AND ts=%s""", (source_code, event_dt)).fetchone()["n"]
    assert at_same_instant == 2

    event_conflict = {**event_a, "details": {"origin": "changed"}}
    conflict_batch = {**body, "batch_id": str(uuid.uuid4()), "records": [event_conflict]}
    conflict_result = client.post("/api/v1/ingest/batches", json=conflict_batch, headers=auth)
    assert conflict_result.status_code == 422 and conflict_result.json()["results"][0]["reason_code"] == "CONFLICT"
    retimed = {**event_a, "observed_at": (event_dt + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")}
    retimed_batch = {**body, "batch_id": str(uuid.uuid4()), "records": [retimed]}
    retimed_result = client.post("/api/v1/ingest/batches", json=retimed_batch, headers=auth)
    assert retimed_result.status_code == 422 and retimed_result.json()["results"][0]["reason_code"] == "CONFLICT"
    stored_hash = one("""SELECT c.key_hash FROM source_credentials c JOIN data_sources s USING(source_id)
                       WHERE s.source_code=%s""", source_code)["key_hash"].strip()
    assert stored_hash == hashlib.sha256(token.encode()).hexdigest() and token not in stored_hash
    DB.execute("""UPDATE source_credentials SET revoked_at=now() WHERE key_prefix=%s""", (token.split("_")[1],))
    assert client.post("/api/v1/ingest/batches", json=body, headers=auth).status_code == 403


@test("fresh database: no alerts and no incidents before detection (nothing pre-seeded, no demo fire)")
def _():
    assert one("SELECT count(*) AS n FROM alerts")["n"] == 0 and one("SELECT count(*) AS n FROM incidents")["n"] == 0
    assert client.get("/alerts").json()["count"] == 0
    assert one("SELECT count(*) AS n FROM event_records WHERE details->>'demo' IS NOT NULL")["n"] == 0


@test("POST /alerts/run detects the seeded faults; running it again creates nothing new")
def _():
    first = client.post("/alerts/run")
    assert first.status_code == 200 and first.json()["new_alerts"] >= 8 and first.json()["as_of"]
    assert any("current only up to" in x for x in first.json()["limitations"])
    second = client.post("/alerts/run").json()
    assert second["new_alerts"] == 0 and second["incidents_created"] == 0


@test("GET /dashboard/overview: window, five system tiles and KPI cards in the shape the page reads")
def _():
    r = client.get("/dashboard/overview", params={"hours": 24})
    assert r.status_code == 200
    d = r.json()
    assert {"from", "to", "hours", "as_of"} <= set(d["window"])
    assert [s["type"] for s in d["systems"]] == ["HVAC", "NETWORK", "ACCESS", "CCTV", "FIRE_ALARM"]
    assert all({"type", "status", "assets", "ok", "warning", "critical", "no_data", "unknown"} <= set(s) for s in d["systems"])
    assert {s["status"] for s in d["systems"]} <= {"OK", "WARNING", "CRITICAL", "UNKNOWN"}
    assert sum(s["assets"] for s in d["systems"]) == one("SELECT count(*) AS n FROM assets")["n"]
    keys = {k["key"] for k in d["kpis"]}
    assert {"monitored_assets", "healthy_assets_pct", "alerts_raised", "critical_alerts", "avg_zone_temp",
            "avg_packet_loss", "heartbeat_compliance", "open_incidents"} <= keys
    assert all({"label", "value", "unit", "definition"} <= set(k) for k in d["kpis"])
    open_n = one("SELECT count(*) AS n FROM incidents WHERE status <> 'resolved'")["n"]
    assert next(k for k in d["kpis"] if k["key"] == "open_incidents")["value"] == open_n
    bars = d["incident_bars"]
    assert bars and all({"asset_id", "asset_name", "system_type", "resolved", "warning", "critical", "info"}
                        <= set(row) for row in bars)
    hvac_bars = client.get("/dashboard/overview", params={"hours": 24, "system_type": "HVAC"}).json()["incident_bars"]
    assert hvac_bars and all(row["system_type"] == "HVAC" for row in hvac_bars)
    assert {row["asset_id"]: row for row in bars if row["system_type"] == "HVAC"} == \
        {row["asset_id"]: row for row in hvac_bars}
    short_window_bars = client.get("/dashboard/overview", params={"hours": 1}).json()["incident_bars"]
    assert short_window_bars == bars  # Incident history is not restricted to the telemetry time window.


@test("overview: KPI numbers equal the same counts computed straight from the tables")
def _():
    d = client.get("/dashboard/overview", params={"hours": 72}).json()
    end = one("SELECT max(ts) AS t FROM (SELECT ts FROM telemetry_records UNION ALL SELECT ts FROM event_records) u")["t"]
    n = one("""SELECT count(*) FILTER (WHERE severity IN ('warning','critical')) AS raised,
                      count(*) FILTER (WHERE severity = 'critical') AS crit FROM event_records
               WHERE ts >= %s - interval '72 hours' AND ts < %s + interval '1 second'""", end, end)
    k = {x["key"]: x["value"] for x in d["kpis"]}
    assert k["alerts_raised"] == n["raised"] and k["critical_alerts"] == n["crit"]
    assert k["monitored_assets"] == 12


@test("system_type accepts the page names (NETWORK, ACCESS) and the database codes; unknown value -> 400")
def _():
    for value in ("NETWORK", "NETWORKING", "ACCESS", "ACCESS_CONTROL", "HVAC"):
        assert client.get("/dashboard/overview", params={"system_type": value}).status_code == 200, value
    net = client.get("/dashboard/overview", params={"system_type": "NETWORK"}).json()
    assert next(k for k in net["kpis"] if k["key"] == "monitored_assets")["value"] == 3
    assert client.get("/dashboard/overview", params={"system_type": "PUMPS"}).status_code == 400
    assert client.get("/dashboard/overview", params={"hours": 0}).status_code == 400
    assert client.get("/dashboard/trends", params={"hours": 9999}).status_code == 400


@test("GET /dashboard/trends: aligned series, heartbeats not counted, access_granted is null (not stored), denied matches the table")
def _():
    d = client.get("/dashboard/trends", params={"hours": 72}).json()
    n = len(d["buckets"])
    for key in ("hvac_avg_temp", "bandwidth_avg", "bandwidth_peak", "access_denied"):
        assert len(d[key]) == n, key
    assert all(len(v) == n for v in d["events_by_system"].values())
    assert set(d["events_by_system"]) == {"HVAC", "NETWORK", "ACCESS", "CCTV", "FIRE_ALARM"}
    assert d["access_granted"] is None and d["bandwidth_unit"] == "%"
    end = one("SELECT max(ts) AS t FROM (SELECT ts FROM telemetry_records UNION ALL SELECT ts FROM event_records) u")["t"]
    cond = "ts >= %s - interval '72 hours' AND ts < %s + interval '1 second'"
    denied = one(f"""SELECT count(*) AS n FROM event_records x JOIN event_types t USING (event_type_id)
                     WHERE t.name='access_denied' AND {cond}""", end, end)["n"]
    non_hb = one(f"""SELECT count(*) AS n FROM event_records x JOIN event_types t USING (event_type_id)
                     WHERE t.name <> 'heartbeat' AND {cond}""", end, end)["n"]
    assert sum(d["access_denied"]) == denied > 0
    assert sum(sum(v) for v in d["events_by_system"].values()) == non_hb
    hot = max(v for v in d["hvac_avg_temp"] if v is not None)
    assert hot > 17, "HVAC temperature series must come from telemetry_records"


@test("trends: the S1 CRAC fault is visible as a temperature rise in the telemetry the chart is drawn from")
def _():
    d = client.get("/dashboard/trends", params={"hours": 72, "system_type": "HVAC"}).json()
    assert list(d["events_by_system"]) == ["HVAC"]
    peak = one("""SELECT max(value) AS v FROM telemetry_records t JOIN metric_types m USING (metric_id)
                  WHERE m.name='temperature' AND t.asset_id = 2""")["v"]
    assert peak > 30 and max(v for v in d["hvac_avg_temp"] if v is not None) > 18


@test("GET /dashboard/equipment-health: recovered history stays visible without an active critical; active alerts stay critical")
def _():
    d = client.get("/dashboard/equipment-health", params={"hours": 72}).json()
    assert len(d["items"]) == 12 and d["window"]["to"]
    need = {"name", "system_type", "building", "floor", "zone", "status", "reason", "events", "faults", "warnings",
            "criticals", "last_seen"}
    assert all(need <= set(i) for i in d["items"])
    rank = {"CRITICAL": 0, "WARNING": 1, "UNKNOWN": 2, "OK": 3}
    assert [rank[i["status"]] for i in d["items"]] == sorted(rank[i["status"]] for i in d["items"])

    # Use a dedicated fixture so this check has the same meaning whether it runs
    # before or after another test has created active alerts on the seeded CRAC.
    base_asset = one("""SELECT a.location_id, a.system_type_id FROM assets a
                       JOIN system_types st USING(system_type_id) WHERE st.code='HVAC' LIMIT 1""")
    fixture_name = f"Recovered Test Asset {uuid.uuid4().hex[:8]}"
    fixture_code = f"TEST-RECOVERED-{uuid.uuid4().hex[:8]}"
    fixture_id = one("""INSERT INTO assets(asset_code,name,asset_type,system_type_id,location_id)
                       VALUES (%s,%s,'sensor',%s,%s) RETURNING asset_id""",
                     fixture_code, fixture_name, base_asset["system_type_id"], base_asset["location_id"])["asset_id"]
    fault_id = one("SELECT event_type_id FROM event_types WHERE name='hvac_fault'")["event_type_id"]
    recovery_id = one("SELECT event_type_id FROM event_types WHERE name='hvac_recovered'")["event_type_id"]
    anchor = one("SELECT max(ts) AS t FROM (SELECT ts FROM telemetry_records UNION ALL SELECT ts FROM event_records) u")["t"]
    fault_ts, recovery_ts = anchor + timedelta(seconds=1), anchor + timedelta(minutes=1)
    DB.execute("""INSERT INTO event_records(asset_id,event_type_id,ts,severity,details)
                   VALUES (%s,%s,%s,'critical','{}'::jsonb),(%s,%s,%s,'info','{}'::jsonb)""",
               (fixture_id, fault_id, fault_ts, fixture_id, recovery_id, recovery_ts))
    try:
        refreshed = client.get("/dashboard/equipment-health", params={"hours": 72}).json()
        recovered = next(i for i in refreshed["items"] if i["name"] == fixture_name)
        assert recovered["status"] != "CRITICAL" and recovered["active_criticals"] == 0
        assert recovered["criticals"] == 1 and recovered["faults"] == 1

        crac = next(i for i in refreshed["items"] if i["name"] == "CRAC Unit 1")
        open_critical_alerts = one("""SELECT count(*) AS n FROM alerts WHERE asset_id=%s
                                    AND severity='critical' AND status IN ('open','acknowledged')""",
                                   next(i["asset_id"] for i in refreshed["items"] if i["name"] == "CRAC Unit 1"))["n"]
        assert (crac["status"] == "CRITICAL") == (open_critical_alerts > 0)
        assert crac["active_criticals"] >= open_critical_alerts
    finally:
        DB.execute("DELETE FROM event_records WHERE asset_id=%s", (fixture_id,))
        DB.execute("DELETE FROM assets WHERE asset_id=%s", (fixture_id,))

    healthy = next(i for i in d["items"] if i["name"] == "Lobby Camera")
    assert healthy["status"] == "OK" and healthy["last_seen"]
    only = client.get("/dashboard/equipment-health", params={"hours": 72, "system_type": "CCTV"}).json()
    assert {i["system_type"] for i in only["items"]} == {"CCTV"} and len(only["items"]) == 2


@test("status semantics: never-reported assets are Unknown; stale previously reported assets are Warning")
def _():
    anchor = one("SELECT max(ts) AS t FROM (SELECT ts FROM telemetry_records UNION ALL SELECT ts FROM event_records) u")["t"]
    location_id = one("SELECT location_id FROM locations ORDER BY location_id LIMIT 1")["location_id"]
    system_id = one("SELECT system_type_id FROM system_types WHERE code='HVAC'")["system_type_id"]
    never_id = one("""INSERT INTO assets (asset_code,name,asset_type,system_type_id,location_id)
                       VALUES ('TEST-UNKNOWN','Never Reported Test Asset','sensor',%s,%s) RETURNING asset_id""",
                   system_id, location_id)["asset_id"]
    stale_id = one("""INSERT INTO assets (asset_code,name,asset_type,system_type_id,location_id)
                      VALUES ('TEST-STALE','Stale Test Asset','sensor',%s,%s) RETURNING asset_id""",
                   system_id, location_id)["asset_id"]
    try:
        metric_id = one("SELECT metric_id FROM metric_types WHERE name='temperature'")["metric_id"]
        DB.execute("INSERT INTO telemetry_records (asset_id,metric_id,ts,value) VALUES (%s,%s,%s,%s)",
                   (stale_id, metric_id, anchor - timedelta(hours=2), 22.0))
        d = client.get("/dashboard/equipment-health", params={"hours": 1, "system_type": "HVAC"}).json()
        by_name = {item["name"]: item for item in d["items"]}
        assert by_name["Never Reported Test Asset"]["status"] == "UNKNOWN"
        assert by_name["Stale Test Asset"]["status"] == "WARNING"
        overview = client.get("/dashboard/overview", params={"hours": 1}).json()
        hvac = next(system for system in overview["systems"] if system["type"] == "HVAC")
        assert hvac["unknown"] >= 1 and hvac["no_data"] >= 1 and hvac["status"] != "OK"
        chat = client.post("/chat", json={"message": "What is the current building status?", "hours": 1,
                                          "use_llm": False}).json()
        assert chat["source"] == "DATABASE" and "1 unknown" in chat["answer"]
    finally:
        DB.execute("DELETE FROM telemetry_records WHERE asset_id = ANY(%s)", ([stale_id, never_id],))
        DB.execute("DELETE FROM assets WHERE asset_id = ANY(%s)", ([stale_id, never_id],))


@test("GET /alerts: stored alerts, newest first, UI names and upper-case enums; filters work")
def _():
    d = client.get("/alerts", params={"limit": 50}).json()
    assert d["count"] == one("SELECT count(*) AS n FROM alerts")["n"] == len(d["items"])
    need = {"id", "severity", "title", "first_ts", "system_type", "asset_name", "zone", "summary", "status", "incident_id"}
    assert all(need <= set(a) for a in d["items"])
    assert {a["severity"] for a in d["items"]} <= {"INFO", "WARNING", "CRITICAL"}
    assert {a["system_type"] for a in d["items"]} <= {"HVAC", "NETWORK", "ACCESS", "CCTV", "FIRE_ALARM"}
    times = [a["first_ts"] for a in d["items"]]
    assert times == sorted(times, reverse=True)
    crit = client.get("/alerts", params={"severity": "CRITICAL"}).json()["items"]
    assert crit and all(a["severity"] == "CRITICAL" for a in crit)
    net = client.get("/alerts", params={"system_type": "NETWORK"}).json()["items"]
    assert net and all(a["system_type"] == "NETWORK" for a in net)
    assert client.get("/alerts", params={"severity": "BOGUS"}).status_code == 400
    assert client.get("/alerts", params={"limit": 0}).status_code == 422


@test("GET /alerts/{id}: evidence rows carry source references; unknown id -> 404")
def _():
    a = client.get("/alerts", params={"limit": 50}).json()["items"]
    crac = next(x for x in a if x["rule_id"] == "HVAC_HIGH_TEMP")
    one_alert = client.get(f"/alerts/{crac['id']}").json()
    ev = one_alert["evidence"]["events"]
    assert ev and all(e["source"] == "telemetry" and e["telemetry_id"] == e["record_id"] for e in ev)
    assert client.get("/alerts/999999").status_code == 404


@test("incidents: list, evidence verified, explanation grounded; the page's three calls work together")
def _():
    inc_list = client.get("/incidents").json()["incidents"]
    assert inc_list and inc_list[0]["severity"] == "critical"
    crac = next(i for i in inc_list if "CRAC" in i["title"])
    detail = client.get(f"/incidents/{crac['id']}").json()
    assert detail["recommended_actions"] and detail["grouping_rule"] == "same_asset_or_system_within_15_min"
    ev = client.get(f"/incidents/{crac['id']}/evidence").json()
    assert ev["total"] > 0 and ev["verified"] == ev["total"] and ev["unverified"] == []
    alert_id = detail["alert_ids"][0]
    ex = client.post(f"/alerts/{alert_id}/explain", params={"use_llm": "false"}).json()
    assert ex["validation"]["status"] == "grounding_supported" and ex["validation"]["scope"] == "grounding"
    assert ex["evidence_verification"]["unverified"] == 0 and ex["recommendations"]
    assert all(r["status"] == "provisional" and not r["approver"] and not r["approval_reference"]
               for r in ex["recommendations"])


@test("incident lifecycle over HTTP: open -> acknowledged -> resolved; resolved is final (409); unknown -> 404")
def _():
    incident = client.get("/incidents").json()["incidents"][0]
    inc_id = incident["id"]
    assert incident["severity"] == "critical"
    asset_id = one("SELECT asset_id FROM alerts WHERE incident_id=%s ORDER BY first_ts, alert_id LIMIT 1", inc_id)["asset_id"]
    before = client.get("/dashboard/overview", params={"hours": 1}).json()["incident_bars"]
    before_bar = next(row for row in before if row["asset_id"] == asset_id)
    assert before_bar["critical"] >= 1
    r = client.post(f"/incidents/{inc_id}/status", json={"status": "acknowledged"})
    assert r.status_code == 200 and r.json()["status"] == "acknowledged"
    assert client.post(f"/incidents/{inc_id}/status", json={"status": "resolved"}).json()["status"] == "resolved"
    assert one("SELECT count(*) AS n FROM alerts WHERE incident_id=%s AND status <> 'resolved'", inc_id)["n"] == 0
    after = client.get("/dashboard/overview", params={"hours": 1}).json()["incident_bars"]
    after_bar = next(row for row in after if row["asset_id"] == asset_id)
    assert after_bar["resolved"] == before_bar["resolved"] + 1
    assert after_bar["critical"] == before_bar["critical"] - 1
    assert client.post(f"/incidents/{inc_id}/status", json={"status": "open"}).status_code == 409
    assert client.post("/incidents/999999/status", json={"status": "resolved"}).status_code == 404
    kpi = client.get("/dashboard/overview").json()["kpis"]
    assert next(k for k in kpi if k["key"] == "open_incidents")["value"] == \
        one("SELECT count(*) AS n FROM incidents WHERE status <> 'resolved'")["n"]


@test("alerts and tampered evidence: a changed fact row is reported as unverified by the explain call")
def _():
    a = next(x for x in client.get("/alerts", params={"limit": 50}).json()["items"] if x["rule_id"] == "HVAC_HIGH_TEMP")
    tid = client.get(f"/alerts/{a['id']}").json()["evidence"]["events"][0]["telemetry_id"]
    DB.execute("UPDATE telemetry_records SET value = value + 5 WHERE telemetry_id = %s", (tid,))
    try:
        ex = client.post(f"/alerts/{a['id']}/explain", params={"use_llm": "false"}).json()
    finally:
        DB.execute("UPDATE telemetry_records SET value = value - 5 WHERE telemetry_id = %s", (tid,))
    assert ex["evidence_verification"]["unverified"] >= 1 and ex["validation"]["status"] == "failed"


@test("chat widget: suggested questions; HVAC question answered from evidence; incident list; unsupported topic is said plainly")
def _():
    q = client.get("/chat/questions").json()
    assert q["questions"]
    hv = client.post("/chat", json={"message": "Why is CRAC 1 temperature high?", "use_llm": False}).json()
    assert hv["source"] == "FALLBACK" and hv["grounding"]["passed"] is True and hv["sources"]
    assert all("verified in database" in s["text"] for s in hv["sources"]) and "[F1]" in hv["answer"]
    from app.domain.alerts.alert_config import T
    latest_crac = one("""SELECT a.name, l.name AS zone FROM telemetry_records t
                        JOIN assets a USING (asset_id) JOIN locations l USING (location_id)
                        JOIN metric_types m USING (metric_id) JOIN system_types st USING (system_type_id)
                        WHERE st.code='HVAC' AND a.name ILIKE %s AND m.name='temperature'
                        ORDER BY t.ts DESC, t.telemetry_id DESC LIMIT 1""", "%crac%")
    configured_threshold = T["server_temp_warn"] if latest_crac["zone"] == "Server Room" else T["hvac_temp_warn"]
    assert latest_crac["name"] in hv["answer"] and f"{configured_threshold:g}C" in hv["answer"]
    assert "configured" in hv["answer"] and "hardcoded" not in hv["answer"].lower()
    inc = client.post("/chat", json={"message": "What is wrong with the building?", "use_llm": False}).json()
    assert inc["answer"].startswith("Open incidents") and "CRAC" in inc["answer"]
    other = client.post("/chat", json={"message": "tell me a joke please", "use_llm": False}).json()
    assert other["source"] == "RULES" and other["grounding"]["passed"] is False
    assert client.post("/chat", json={"message": "x"}).status_code == 422


@test("pages: / and /dashboard serve the dashboard; /static files are served; /health answers")
def _():
    for path in ("/", "/dashboard"):
        r = client.get(path)
        assert r.status_code == 200 and "Building operations dashboard" in r.text, path
    assert client.get("/static/style.css").status_code == 200 and client.get("/static/chart.umd.js").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}
    html = client.get("/").text
    assert 'api("overview")' in html and "/alerts?limit=200" in html


@test("database down: every page endpoint answers 503 with a message instead of crashing")
def _():
    saved = os.environ["PGPORT"]
    os.environ["PGPORT"] = "1"
    try:
        calls = [client.get("/dashboard/overview"), client.get("/dashboard/trends"),
                 client.get("/dashboard/equipment-health"), client.get("/alerts"), client.get("/incidents"),
                 client.post("/alerts/run"), client.post("/chat", json={"message": "crac temperature"})]
        assert [c.status_code for c in calls] == [503] * len(calls), [c.status_code for c in calls]
    finally:
        os.environ["PGPORT"] = saved


@test("readiness: /ready is 200 while the database answers and 503 when it is unreachable; /health stays liveness-only")
def _():
    up = client.get("/ready")
    assert up.status_code == 200 and up.json() == {"status": "ready", "database": "up"}, up.text
    saved = os.environ["PGPORT"]
    os.environ["PGPORT"] = "1"
    try:
        down = client.get("/ready")
        assert down.status_code == 503 and down.json() == {"status": "not_ready", "database": "down"}, down.text
        assert client.get("/health").json() == {"status": "ok"}
    finally:
        os.environ["PGPORT"] = saved


@test("legacy POST /events and POST /alerts remain available alongside v1 ingestion")
def _():
    asset = one("""SELECT a.asset_id FROM assets a JOIN system_types st USING(system_type_id)
                   WHERE st.code='HVAC' ORDER BY a.asset_id LIMIT 1""")
    ts = "2001-01-01T00:00:00Z"
    event = client.post("/events", json={"asset_id": asset["asset_id"], "event_type": "hvac_fault",
                                        "ts": ts, "severity": "INFO", "details": {"compat": True}})
    assert event.status_code == 201, event.text
    duplicate_event = client.post("/events", json={"asset_id": asset["asset_id"], "event_type": "hvac_fault",
                                                   "ts": ts, "severity": "INFO", "details": {"compat": True}})
    assert duplicate_event.status_code == 409
    alert = client.post("/alerts", json={"rule_id": "HVAC_HIGH_TEMP", "asset_id": asset["asset_id"],
                                        "severity": "WARNING", "summary": "legacy endpoint compatibility",
                                        "first_ts": ts, "facts": {"compat": True}})
    assert alert.status_code == 201, alert.text


_CV = {}


def _cv_prepare():
    asset, metric, building_code, source_code, token = _ingestion_setup()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    z = lambda value: value.isoformat().replace("+00:00", "Z")
    _CV.update(asset=asset, metric=metric, building_code=building_code, source_code=source_code,
               token=token, auth={"Authorization": f"Bearer {token}"}, now=now, z=z,
               base_ts=z(now - timedelta(days=5)))
    return _CV


def _cv_batch(records, **extra):
    c = _CV
    return {"contract_version": "1.0", "batch_id": str(uuid.uuid4()), "sent_at": c["z"](c["now"]),
            "source": {"source_code": c["source_code"],
                       "connector": {"name": "conformance", "version": "1"}},
            "building_code": c["building_code"], "records": records, **extra}


def _cv_telemetry(ts, value=15.0, **extra):
    c = _CV
    return {"record_type": "telemetry", "asset": {"asset_code": c["asset"]["asset_code"]},
            "observed_at": ts, "metric_code": c["metric"]["name"], "value": value,
            "unit": c["metric"]["unit"], **extra}


def _cv_post(records, **extra):
    payload = _cv_batch(records, **extra)
    return payload, client.post("/api/v1/ingest/batches", json=payload, headers=_CV["auth"])


@test("Vector 1: valid envelope accepts three records")
def _():
    c = _cv_prepare()
    t1 = c["z"](c["now"] - timedelta(days=4))
    records = [_cv_telemetry(t1),
               {"record_type": "event", "asset": {"asset_code": c["asset"]["asset_code"]},
                "observed_at": t1, "source_event_id": "cv-event-1", "event_type": "hvac_fault"},
               {"record_type": "state", "asset": {"asset_code": c["asset"]["asset_code"]},
                "observed_at": t1, "reported_state": "RUNNING"}]
    payload = _cv_batch(records, delivery_mode="BACKFILL")
    response = client.post("/api/v1/ingest/batches", json=payload, headers=c["auth"])
    assert response.status_code == 200 and response.json()["status"] == "ACCEPTED"
    assert response.json()["counts"]["accepted"] == 3
    assert one("SELECT delivery_mode,connector_status FROM ingestion_batches WHERE batch_id=%s",
               payload["batch_id"]) == {"delivery_mode": "BACKFILL", "connector_status": None}
    c.update(t1=t1, three=records, p1=payload)


@test("Vector 2: exact batch replay returns stored result")
def _():
    c = _CV
    first = client.post("/api/v1/ingest/batches", json=c["p1"], headers=c["auth"])
    replay = client.post("/api/v1/ingest/batches", json=c["p1"], headers=c["auth"])
    assert first.status_code == 200 and replay.status_code == 200
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert replay.json() == first.json()


@test("Vector 3: changed payload reusing batch_id returns 409 BATCH_ID_REUSED")
def _():
    c = _CV
    changed = {**c["p1"], "records": [*c["three"][:-1],
               {**c["three"][-1], "reported_state": "STOPPED"}]}
    response = client.post("/api/v1/ingest/batches", json=changed, headers=c["auth"])
    assert response.status_code == 409 and response.json()["error"]["code"] == "BATCH_ID_REUSED"


@test("Vector 4: same telemetry key and value in a new batch is a duplicate")
def _():
    c = _CV
    _, response = _cv_post([_cv_telemetry(c["t1"])])
    assert response.status_code == 200 and response.json()["counts"]["duplicate"] == 1


@test("Vector 5: conflicting telemetry preserves the first value")
def _():
    c = _CV
    _, response = _cv_post([_cv_telemetry(c["t1"], value=17.0)])
    assert response.status_code == 422 and response.json()["results"][0]["reason_code"] == "CONFLICT"
    stored = one("SELECT value FROM telemetry_records WHERE asset_id=%s AND metric_id=(SELECT metric_id FROM metric_types WHERE name=%s) AND ts=%s",
                 c["asset"]["asset_id"], c["metric"]["name"], datetime.fromisoformat(c["t1"].replace("Z", "+00:00")))
    assert stored["value"] == 15.0


@test("Vector 6: timestamp with a non-UTC offset is INVALID_TIMESTAMP")
def _():
    _, response = _cv_post([_cv_telemetry("2026-11-15T11:15:00+02:00")])
    assert response.status_code == 422 and response.json()["results"][0]["reason_code"] == "INVALID_TIMESTAMP"


@test("Vector 7: timestamp ten minutes in the future is FUTURE_TIMESTAMP")
def _():
    c = _CV
    ts = c["z"](datetime.now(timezone.utc) + timedelta(minutes=10))
    _, response = _cv_post([_cv_telemetry(ts)])
    assert response.status_code == 422 and response.json()["results"][0]["reason_code"] == "FUTURE_TIMESTAMP"


@test("Vector 8: door_forced on a camera is EVENT_NOT_COMPATIBLE")
def _():
    c = _CV
    camera = one("""SELECT a.asset_code FROM assets a JOIN system_types st USING(system_type_id)
                    WHERE st.code='CCTV' ORDER BY a.asset_id LIMIT 1""")
    record = {"record_type": "event", "asset": {"asset_code": camera["asset_code"]},
              "observed_at": c["base_ts"], "source_event_id": "cv-door-for-camera", "event_type": "door_forced"}
    _, response = _cv_post([record])
    assert response.status_code == 422 and response.json()["results"][0]["reason_code"] == "EVENT_NOT_COMPATIBLE"


@test("Vector 9: Fahrenheit telemetry is rejected with UNIT_MISMATCH")
def _():
    c = _CV
    _, response = _cv_post([_cv_telemetry(c["z"](c["now"] - timedelta(days=3)), unit="F")])
    assert response.status_code == 422 and response.json()["results"][0]["reason_code"] == "UNIT_MISMATCH"


@test("Vector 10: unmapped external ID is rejected and audited")
def _():
    c = _CV
    external_id = "vendor-unmapped-" + uuid.uuid4().hex
    record = {**_cv_telemetry(c["z"](c["now"] - timedelta(days=2))), "asset": {"external_id": external_id}}
    _, response = _cv_post([record])
    assert response.status_code == 422 and response.json()["results"][0]["reason_code"] == "ASSET_UNRESOLVED"
    rejected = one("SELECT source_id,external_id FROM ingestion_rejections WHERE external_id=%s", external_id)
    assert rejected and rejected["external_id"] == external_id
    view = one("SELECT count,first_seen,last_seen FROM v_unmapped_external_assets WHERE source=%s AND external_id=%s",
               c["source_code"], external_id)
    assert view and view["count"] == 1 and view["first_seen"]


@test("Vector 11: quality MISSING is SCHEMA_INVALID")
def _():
    c = _CV
    record = {**_cv_telemetry(c["z"](c["now"] - timedelta(days=1))), "quality": "MISSING"}
    _, response = _cv_post([record])
    assert response.status_code == 422 and response.json()["results"][0]["reason_code"] == "SCHEMA_INVALID"


@test("Vector 12: state with only communication_status is SCHEMA_INVALID")
def _():
    c = _CV
    record = {"record_type": "state", "asset": {"asset_code": c["asset"]["asset_code"]},
              "observed_at": c["base_ts"], "communication_status": "OFFLINE"}
    _, response = _cv_post([record])
    assert response.status_code == 422 and response.json()["results"][0]["reason_code"] == "SCHEMA_INVALID"


@test("Vector 13: 1000 records with ten invalid records return PARTIAL")
def _():
    c = _CV
    start = c["now"] - timedelta(days=30)
    records = []
    for i in range(1000):
        record = _cv_telemetry(c["z"](start + timedelta(seconds=i)), value=100.0 + i)
        if i % 100 == 0:
            record["unit"] = "not-a-unit"
        records.append(record)
    _, response = _cv_post(records)
    assert response.status_code == 200 and response.json()["status"] == "PARTIAL"
    assert response.json()["counts"] == {"received": 1000, "accepted": 990, "duplicate": 0, "rejected": 10}


@test("Vector 14: 5001 records return 413 BATCH_TOO_LARGE")
def _():
    _, response = _cv_post([{} for _ in range(5001)])
    assert response.status_code == 413 and response.json()["error"]["code"] == "BATCH_TOO_LARGE"


@test("Vector 15: wrong source_code for key returns 403 SOURCE_MISMATCH")
def _():
    c = _CV
    payload = _cv_batch([], source={"source_code": "wrong-source",
                                    "connector": {"name": "conformance", "version": "1"}})
    response = client.post("/api/v1/ingest/batches", json=payload, headers=c["auth"])
    assert response.status_code == 403 and response.json()["error"]["code"] == "SOURCE_MISMATCH"


@test("Vector 16: empty valid batch returns 200 and updates source liveness")
def _():
    c = _CV
    before = one("SELECT last_received_at FROM data_sources WHERE source_code=%s", c["source_code"])["last_received_at"]
    payload, response = _cv_post([])
    after = one("SELECT last_received_at FROM data_sources WHERE source_code=%s", c["source_code"])["last_received_at"]
    assert response.status_code == 200 and response.json()["status"] == "ACCEPTED"
    assert after >= before
    assert one("SELECT delivery_mode FROM ingestion_batches WHERE batch_id=%s", payload["batch_id"])["delivery_mode"] == "LIVE"


@test("Step 0.4: auth, scopes, reason codes, back-date, diagnostics, and plausibility")
def _():
    asset, metric, building_code, source_code, token = _ingestion_setup()
    auth = {"Authorization": f"Bearer {token}"}
    now = datetime.now(timezone.utc).replace(microsecond=0)
    z = lambda value: value.isoformat().replace("+00:00", "Z")
    def batch(records, **extra):
        return {"contract_version": "1.0", "batch_id": str(uuid.uuid4()), "sent_at": z(now),
                "source": {"source_code": source_code, "connector": {"name": "conformance", "version": "1"}},
                "building_code": building_code, "records": records, **extra}
    def telemetry(ts, value=15.0, **extra):
        return {"record_type": "telemetry", "asset": {"asset_code": asset["asset_code"]},
                "observed_at": ts, "metric_code": metric["name"], "value": value,
                "unit": metric["unit"], **extra}
    def post(records, **extra):
        payload = batch(records, **extra)
        return payload, client.post("/api/v1/ingest/batches", json=payload, headers=auth)
    p16, r16 = post([])
    event = {"record_type": "event", "asset": {"asset_code": asset["asset_code"]},
             "observed_at": z(now - timedelta(days=5)), "source_event_id": "step04-event",
             "event_type": "hvac_fault"}
    # Additional Step 0.4 conformance: auth, scope, code distinctions, time override, delivery diagnostics, quality.
    missing_auth = client.post("/api/v1/ingest/batches", json=p16)
    invalid_auth = client.post("/api/v1/ingest/batches", json=p16, headers={"Authorization": "Bearer invalid"})
    assert missing_auth.status_code == 401 and missing_auth.json()["error"]["code"] == "AUTH_MISSING"
    assert invalid_auth.status_code == 401 and invalid_auth.json()["error"]["code"] == "AUTH_INVALID"
    DB.execute("UPDATE source_credentials SET scopes=ARRAY['ingest:telemetry']::text[] WHERE key_prefix=%s", (token.split("_")[1],))
    _, scope_response = post([event])
    assert scope_response.json()["results"][0]["reason_code"] == "SCOPE_DENIED"
    DB.execute("UPDATE source_credentials SET scopes=ARRAY['ingest:telemetry','ingest:event','ingest:state']::text[] WHERE key_prefix=%s", (token.split("_")[1],))

    _, unknown_metric = post([{**telemetry(z(now - timedelta(hours=1))), "metric_code": "unknown_metric"}])
    assert unknown_metric.json()["results"][0]["reason_code"] == "UNKNOWN_METRIC"
    _, incompatible_metric = post([{**telemetry(z(now - timedelta(hours=2))), "metric_code": "camera_fps", "unit": "fps"}])
    assert incompatible_metric.json()["results"][0]["reason_code"] == "METRIC_NOT_COMPATIBLE"
    _, unknown_event = post([{**event, "source_event_id": "cv-unknown-event", "event_type": "not_registered"}])
    assert unknown_event.json()["results"][0]["reason_code"] == "UNKNOWN_EVENT_TYPE"

    other_building_asset = one("""SELECT a.asset_code FROM assets a JOIN locations l USING(location_id)
                                WHERE l.building_id<>%s LIMIT 1""", asset["building_id"])
    _, not_permitted = post([{**telemetry(z(now - timedelta(hours=3))), "asset": {"asset_code": other_building_asset["asset_code"]}}])
    assert not_permitted.json()["results"][0]["reason_code"] == "ASSET_NOT_PERMITTED"

    too_old = z(now - timedelta(days=91))
    _, old_default = post([telemetry(too_old)])
    assert old_default.json()["results"][0]["reason_code"] == "TIMESTAMP_TOO_OLD"
    _, at_default_limit = post([telemetry(z(now - timedelta(days=89, hours=23)))])
    assert at_default_limit.status_code == 200 and at_default_limit.json()["counts"]["accepted"] == 1, at_default_limit.text
    config = subprocess.run([sys.executable, "scripts/ingestion_admin.py", "set-backdate-limit",
                             "--source-code", source_code, "--max-backdate-days", "120"],
                            capture_output=True, text=True, env=os.environ.copy(), check=True)
    assert "120 days" in config.stdout
    _, override_ok = post([telemetry(too_old)])
    assert override_ok.status_code == 200 and override_ok.json()["counts"]["accepted"] == 1
    reset = subprocess.run([sys.executable, "scripts/ingestion_admin.py", "set-backdate-limit",
                            "--source-code", source_code, "--use-default"],
                           capture_output=True, text=True, env=os.environ.copy(), check=True)
    assert "default (90 days)" in reset.stdout
    assert one("SELECT max_backdate_days FROM data_sources WHERE source_code=%s", source_code)["max_backdate_days"] is None

    connector_report = {"vendor_link": "CONNECTED", "buffered_records": 2, "dropped_records": 0}
    p_status, r_status = post([], delivery_mode="BACKFILL", source={"source_code": source_code,
                      "connector": {"name": "conformance", "version": "1"}, "connector_status": connector_report})
    assert r_status.status_code == 200
    stored_status = one("SELECT delivery_mode,connector_status FROM ingestion_batches WHERE batch_id=%s", p_status["batch_id"])
    assert stored_status["delivery_mode"] == "BACKFILL" and stored_status["connector_status"] == connector_report
    DB.execute("UPDATE data_sources SET status='DEGRADED' WHERE source_code=%s", (source_code,))
    _, degraded_batch = post([])
    assert degraded_batch.status_code == 200
    DB.execute("UPDATE data_sources SET status='ACTIVE' WHERE source_code=%s", (source_code,))

    DB.execute("UPDATE metric_types SET min_plausible=0,max_plausible=50 WHERE metric_id=(SELECT metric_id FROM metric_types WHERE name=%s)",
               (metric["name"],))
    p_bad, r_bad = post([telemetry(z(now - timedelta(minutes=3)), value=80),
                         telemetry(z(now - timedelta(minutes=4)), value=-1)])
    assert r_bad.status_code == 200 and r_bad.json()["counts"]["accepted"] == 2
    assert sum(w["code"] == "OUT_OF_PLAUSIBLE_RANGE" for w in r_bad.json()["warnings"]) == 2
    bad_rows = one("SELECT count(*) AS n FROM telemetry_records WHERE ingestion_batch_id=(SELECT ingestion_batch_id FROM ingestion_batches WHERE batch_id=%s) AND quality='BAD'",
                   p_bad["batch_id"])
    assert bad_rows["n"] == 2
    assert one("SELECT count(*) AS n FROM v_telemetry_valid WHERE ingestion_batch_id=(SELECT ingestion_batch_id FROM ingestion_batches WHERE batch_id=%s)",
               p_bad["batch_id"])["n"] == 0

    # Immediate response is capped, while GET returns the complete rejection audit.
    many_bad = [telemetry(z(now - timedelta(days=1) + timedelta(seconds=i)), unit="bad") for i in range(101)]
    p_cap, r_cap = post(many_bad)
    assert r_cap.status_code == 422 and len(r_cap.json()["rejected"]) == 100 and r_cap.json()["rejected_truncated"] is True
    full = client.get(f"/api/v1/ingest/batches/{p_cap['batch_id']}", headers=auth)
    assert full.status_code == 200 and len(full.json()["rejected"]) == 101 and full.json()["rejected_truncated"] is False


@test("source health: received always advances; only success/duplicate/empty advances success; first error is capped")
def _():
    asset, metric, building_code, source_code, token = _ingestion_setup()
    auth = {"Authorization": f"Bearer {token}"}
    observed_at = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0)

    def stamp(value):
        return value.isoformat().replace("+00:00", "Z")

    def telemetry(*, metric_code=None, unit=None):
        return {"record_type": "telemetry", "asset": {"asset_code": asset["asset_code"]},
                "observed_at": stamp(observed_at), "metric_code": metric_code or metric["name"],
                "value": 12.0, "unit": metric["unit"] if unit is None else unit}

    def post(records):
        payload = {"contract_version": "1.0", "batch_id": str(uuid.uuid4()),
                   "sent_at": stamp(datetime.now(timezone.utc)),
                   "source": {"source_code": source_code,
                              "connector": {"name": "source-health-test", "version": "1"}},
                   "building_code": building_code, "records": records}
        return client.post("/api/v1/ingest/batches", json=payload, headers=auth)

    def health():
        return one("""SELECT last_received_at,last_success_at,last_error_at,last_error
                     FROM data_sources WHERE source_code=%s""", source_code)

    accepted = post([telemetry()])
    assert accepted.status_code == 200 and accepted.json()["counts"]["accepted"] == 1
    after_accepted = health()
    assert after_accepted["last_received_at"] is not None
    assert after_accepted["last_success_at"] == after_accepted["last_received_at"]
    assert after_accepted["last_error_at"] is None and after_accepted["last_error"] is None

    duplicate = post([telemetry()])
    assert duplicate.status_code == 200 and duplicate.json()["counts"]["duplicate"] == 1
    after_duplicate = health()
    assert after_duplicate["last_received_at"] > after_accepted["last_received_at"]
    assert after_duplicate["last_success_at"] == after_duplicate["last_received_at"]

    rejected = post([telemetry(unit="x" * 650), telemetry(metric_code="unknown_metric")])
    assert rejected.status_code == 422 and rejected.json()["counts"]["rejected"] == 2
    first = rejected.json()["results"][0]
    after_rejected = health()
    assert after_rejected["last_received_at"] > after_duplicate["last_received_at"]
    assert after_rejected["last_success_at"] == after_duplicate["last_success_at"]
    assert after_rejected["last_error_at"] == after_rejected["last_received_at"]
    assert after_rejected["last_error"] == f"{first['reason_code']}: {first['message']}"[:500]
    assert len(after_rejected["last_error"]) == 500

    empty = post([])
    assert empty.status_code == 200 and empty.json()["counts"] == {
        "received": 0, "accepted": 0, "duplicate": 0, "rejected": 0}
    after_empty = health()
    assert after_empty["last_received_at"] > after_rejected["last_received_at"]
    assert after_empty["last_success_at"] == after_empty["last_received_at"]


@test("ingestion request validation: key shape gates errors, field path only, other routes keep default envelope")
def _():
    marker = "request-input-must-not-be-echoed"
    payload = {"contract_version": "1.0", "batch_id": marker,
               "sent_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
               "source": {"source_code": "validation-test-source",
                          "connector": {"name": "validation-test", "version": "1"}},
               "building_code": "VALIDATION-TEST", "records": []}

    missing = TestClient(App.api).post("/api/v1/ingest/batches", json=payload)
    invalid = TestClient(App.api).post("/api/v1/ingest/batches", json=payload,
                                      headers={"Authorization": "Bearer not-a-source-key"})
    key_shaped = "azk_0123456789ab_" + "s" * 40
    malformed = TestClient(App.api).post("/api/v1/ingest/batches", json=payload,
                                        headers={"Authorization": f"Bearer {key_shaped}"})

    assert missing.status_code == 401 and missing.json()["error"]["code"] == "AUTH_MISSING"
    assert invalid.status_code == 401 and invalid.json()["error"]["code"] == "AUTH_INVALID"
    assert set(missing.json()) == {"error"} and set(missing.json()["error"]) == {"code", "message", "retryable"}
    assert malformed.status_code == 422 and malformed.json()["error"]["code"] == "SCHEMA_INVALID"
    assert "batch_id" in malformed.json()["error"]["message"]
    assert marker not in malformed.text and "input" not in malformed.json()

    other_route = client.get("/dashboard/overview", params={"hours": "not-an-integer"})
    assert other_route.status_code == 422 and "detail" in other_route.json() and "error" not in other_route.json()


@test("ingestion rate limit: auth-scoped buckets, retry without batch consumption, and no rejected writes")
def _():
    class FakeClock:
        now = 1000.0

        def __call__(self):
            return self.now

    clock = FakeClock()
    prior_limiter = App.rate_limit.ingestion_rate_limiter
    App.rate_limit.ingestion_rate_limiter = InMemoryTokenBucket(
        RateLimitSettings(per_minute=1, burst=1), clock=clock)
    try:
        asset1, metric1, building1, source1, token1 = _ingestion_setup()
        asset2, metric2, building2, source2, token2 = _ingestion_setup()

        def payload(source_code, building_code, connector, metric, asset, stamp, batch_id):
            return {"contract_version": "1.0", "batch_id": batch_id,
                    "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "source": {"source_code": source_code,
                               "connector": {"name": connector, "version": "1"}},
                    "building_code": building_code, "records": [{
                        "record_type": "telemetry", "asset": {"asset_code": asset["asset_code"]},
                        "observed_at": stamp, "metric_code": metric["name"], "value": 16.0,
                        "unit": metric["unit"]}]}

        def stamp(minutes):
            return (datetime.now(timezone.utc) - timedelta(hours=3, minutes=minutes)).replace(
                microsecond=0).isoformat().replace("+00:00", "Z")

        batch_id1 = str(uuid.uuid4())
        first = payload(source1, building1, "limit-source-1", metric1, asset1, stamp(1), batch_id1)
        under_limit = client.post("/api/v1/ingest/batches", json=first,
                                  headers={"Authorization": f"Bearer {token1}"})
        assert under_limit.status_code == 200 and under_limit.json()["counts"]["accepted"] == 1

        retry_id = str(uuid.uuid4())
        retry_body = payload(source1, building1, "limit-source-1", metric1, asset1, stamp(2), retry_id)
        counts_sql = """SELECT (SELECT count(*) FROM ingestion_batches) AS batches,
                               (SELECT count(*) FROM telemetry_records) AS telemetry,
                               (SELECT count(*) FROM event_records) AS events,
                               (SELECT count(*) FROM equipment_state_records) AS states"""
        before_counts = one(counts_sql)
        before_health = one("""SELECT last_received_at,last_success_at,last_error_at,last_error
                              FROM data_sources WHERE source_code=%s""", source1)
        limited = client.post("/api/v1/ingest/batches", json=retry_body,
                              headers={"Authorization": f"Bearer {token1}"})
        assert limited.status_code == 429
        assert limited.headers.get("Retry-After") and int(limited.headers["Retry-After"]) >= 1
        assert limited.json() == {"error": {"code": "RATE_LIMITED",
                                            "message": "Ingestion rate limit exceeded; retry after the indicated delay.",
                                            "retryable": True}}
        assert one(counts_sql) == before_counts
        assert one("SELECT count(*) AS n FROM ingestion_batches WHERE batch_id=%s", retry_id)["n"] == 0
        assert one("""SELECT last_received_at,last_success_at,last_error_at,last_error
                      FROM data_sources WHERE source_code=%s""", source1) == before_health

        # A validly shaped but incorrect secret is authenticated before any bucket is consumed.
        bad_token = token2[:-1] + ("A" if token2[-1] != "A" else "B")
        second_id = str(uuid.uuid4())
        second_body = payload(source2, building2, "limit-source-2", metric2, asset2, stamp(3), second_id)
        rejected_key = client.post("/api/v1/ingest/batches", json=second_body,
                                   headers={"Authorization": f"Bearer {bad_token}"})
        assert rejected_key.status_code == 401 and rejected_key.json()["error"]["code"] == "AUTH_INVALID"
        second_source = client.post("/api/v1/ingest/batches", json=second_body,
                                    headers={"Authorization": f"Bearer {token2}"})
        assert second_source.status_code == 200 and second_source.json()["counts"]["accepted"] == 1

        clock.now += 60.0
        retried = client.post("/api/v1/ingest/batches", json=retry_body,
                              headers={"Authorization": f"Bearer {token1}"})
        assert retried.status_code == 200 and retried.json()["counts"]["accepted"] == 1
        assert retried.json()["batch_id"] == retry_id
    finally:
        App.rate_limit.ingestion_rate_limiter = prior_limiter


@test("ingestion admin audit: JSON lifecycle events, not-found/failure outcomes, and no key/hash disclosure")
def _():
    building_code = "API-INGEST-TEST"
    source_code = f"audit-test-{uuid.uuid4().hex[:10]}"
    env = os.environ.copy()
    env["ANZMA_AUDIT_ACTOR"] = "test-operator"

    def run(*args):
        return subprocess.run([sys.executable, "scripts/ingestion_admin.py", *args],
                              capture_output=True, text=True, env=env)

    def audit_records(result):
        found = []
        for line in result.stderr.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event", "").startswith("KEY_"):
                found.append((line, record))
        return found

    created = run("create-source", "--source-code", source_code, "--building-code", building_code,
                  "--connector-name", "audit-test", "--connector-version", "1")
    assert created.returncode == 0
    key1 = re.search(r"API key \(shown once\): (\S+)", created.stdout).group(1)
    prefix1 = key1.split("_")[1]
    create_records = audit_records(created)
    assert len(create_records) == 1
    create_line, create_audit = create_records[0]
    credential1 = one("SELECT credential_id FROM source_credentials WHERE key_prefix=%s", prefix1)["credential_id"]
    assert create_audit == {"timestamp": create_audit["timestamp"], "event": "KEY_CREATED",
                            "source_code": source_code, "key_prefix": prefix1,
                            "credential_id": credential1, "actor": "test-operator",
                            "outcome": "SUCCESS", "host": create_audit["host"]}
    assert create_audit["timestamp"].endswith("Z") and key1 not in create_line
    assert hashlib.sha256(key1.encode()).hexdigest() not in create_line

    rotated = run("rotate-key", "--source-code", source_code)
    assert rotated.returncode == 0
    key2 = re.search(r"New API key for .* \(shown once\): (\S+)", rotated.stdout).group(1)
    prefix2 = key2.split("_")[1]
    rotate_records = audit_records(rotated)
    assert len(rotate_records) == 1
    rotate_line, rotate_audit = rotate_records[0]
    credential2 = one("SELECT credential_id FROM source_credentials WHERE key_prefix=%s", prefix2)["credential_id"]
    assert rotate_audit["event"] == "KEY_ROTATED" and rotate_audit["outcome"] == "SUCCESS"
    assert rotate_audit["source_code"] == source_code and rotate_audit["key_prefix"] == prefix2
    assert rotate_audit["credential_id"] == credential2 and rotate_audit["actor"] == "test-operator"
    assert key2 not in rotate_line and hashlib.sha256(key2.encode()).hexdigest() not in rotate_line

    revoked = run("revoke-key", "--source-code", source_code, "--key-prefix", prefix2)
    assert revoked.returncode == 0
    revoke_records = audit_records(revoked)
    assert len(revoke_records) == 1
    revoke_line, revoke_audit = revoke_records[0]
    assert revoke_audit["event"] == "KEY_REVOKED" and revoke_audit["outcome"] == "SUCCESS"
    assert revoke_audit["source_code"] == source_code and revoke_audit["key_prefix"] == prefix2
    assert revoke_audit["credential_id"] == credential2 and key2 not in revoke_line
    assert hashlib.sha256(key2.encode()).hexdigest() not in revoke_line

    missing = run("revoke-key", "--source-code", source_code, "--key-prefix", "abcdef123456")
    assert missing.returncode != 0
    missing_records = audit_records(missing)
    assert len(missing_records) == 1 and missing_records[0][1]["event"] == "KEY_REVOKED"
    assert missing_records[0][1]["outcome"] == "NOT_FOUND"

    failed_create = run("create-source", "--source-code", source_code, "--building-code", building_code,
                        "--connector-name", "audit-test", "--connector-version", "1")
    assert failed_create.returncode != 0
    failed_records = audit_records(failed_create)
    assert len(failed_records) == 1 and failed_records[0][1]["event"] == "KEY_CREATED"
    assert failed_records[0][1]["outcome"] == "FAILED"


if __name__ == "__main__":
    ok = 0
    for name, fn in RESULTS:
        try:
            fn()
            print("PASS ", name)
            ok += 1
        except Exception as exc:
            print("FAIL ", name, f"\n      -> {type(exc).__name__}: {exc}")
    print(f"\n{ok}/{len(RESULTS)} passed")
    sys.exit(0 if ok == len(RESULTS) else 1)
