"""Read paginated reference events and push canonical batches to Anzma Core."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from connectors.sdk import (CanonicalEvent, ConnectorHealth, ConnectorManifest, ConnectionTest,
                            Page, load_manifest)
from connectors.reference_rest.checkpoint import SyncCheckpointStore


PAGE_LIMIT = 100
MAX_PAGES = 1000
MAX_RETRIES = 3
MANIFEST = load_manifest(Path(__file__).with_name("manifest.json"))
EVENT_TYPES = {"DOOR_OPENED": "door_opened", "ACCESS_GRANTED": "access_granted",
               "OFFLINE_START": "offline_start", "OFFLINE_END": "offline_end"}


class ConnectorError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep bearer credentials scoped to the explicitly configured origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(url: str, token: str, timeout: float = 10.0) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token,
                                                    "Accept": "application/json"})
    for attempt in range(MAX_RETRIES + 1):
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not isinstance(result, dict):
                raise ConnectorError("upstream response must be an object")
            return result
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            if not retryable or attempt == MAX_RETRIES:
                raise ConnectorError(f"upstream returned HTTP {exc.code}") from None
            delay = min(2 ** attempt, 8)
            try:
                delay = min(float(exc.headers.get("Retry-After", delay)), 8)
            except (TypeError, ValueError):
                pass
            time.sleep(max(0, delay))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == MAX_RETRIES:
                raise ConnectorError("upstream request failed after bounded retries") from None
            time.sleep(min(2 ** attempt, 8))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ConnectorError("upstream returned invalid JSON") from None
    raise ConnectorError("upstream request failed")


class ReferenceRESTConnector:
    """Reference implementation of the SDK lifecycle and event-history contracts."""

    manifest: ConnectorManifest = MANIFEST

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def read_events(self, from_utc: str, to_utc: str, *, cursor: str | None = None,
                    limit: int = PAGE_LIMIT,
                    filters: Mapping[str, Any] | None = None) -> Page[CanonicalEvent]:
        _validate_utc_window(from_utc, to_utc)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= PAGE_LIMIT:
            raise ConnectorError(f"limit must be between 1 and {PAGE_LIMIT}")
        if filters:
            raise ConnectorError("the reference connector does not support event filters")
        params = {"from": from_utc, "to": to_utc, "limit": str(limit)}
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor:
                raise ConnectorError("cursor must be a non-empty string or null")
            params["cursor"] = cursor
        payload = _request(self.base_url + "/v1/events?" + urllib.parse.urlencode(params), self.token)
        items = payload.get("items")
        if not isinstance(items, list) or len(items) > limit:
            raise ConnectorError("upstream page omitted items or exceeded the requested limit")
        normalized = []
        for item in items:
            if not isinstance(item, dict):
                raise ConnectorError("upstream page contains a non-object event")
            raw_type = item.get("event_type")
            if not isinstance(raw_type, str):
                raise ConnectorError("upstream event_type must be a string")
            canonical_type = EVENT_TYPES.get(raw_type)
            if canonical_type is None:
                raise ConnectorError("upstream returned an unsupported event type")
            details = item.get("details")
            if details is None:
                details = {}
            if not isinstance(details, dict):
                raise ConnectorError("source event details must be an object")
            try:
                normalized.append(CanonicalEvent(
                    external_event_id=item.get("id"), asset_external_id=item.get("asset_external_id"),
                    event_type=canonical_type, observed_at=item.get("observed_at"),
                    quality=item.get("quality", "UNKNOWN"), received_at=item.get("received_at"),
                    severity=item.get("severity", "INFO"), details=details,
                    source_metadata={"upstream_event_type": raw_type},
                ))
            except ValueError as exc:
                raise ConnectorError(f"invalid canonical source event: {exc}") from None
        next_cursor = payload.get("next_cursor")
        # Missing coverage metadata must never be upgraded to a claim of full
        # history. Require each upstream page to state completeness explicitly.
        if "complete" not in payload or not isinstance(payload["complete"], bool):
            raise ConnectorError("upstream page omitted or provided invalid completeness metadata")
        complete = payload["complete"]
        try:
            return Page(tuple(normalized), next_cursor, complete)
        except ValueError as exc:
            raise ConnectorError(f"invalid upstream page metadata: {exc}") from None

    def test_connection(self) -> ConnectionTest:
        started = time.monotonic()
        checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            # An empty historical window still verifies both endpoint access and read permission.
            self.read_events("1970-01-01T00:00:00Z", "1970-01-02T00:00:00Z", limit=1)
        except ConnectorError as exc:
            return ConnectionTest(False, checked_at, self.manifest.capabilities,
                                  int((time.monotonic() - started) * 1000), "CONNECTION_FAILED", str(exc))
        return ConnectionTest(True, checked_at, self.manifest.capabilities,
                              int((time.monotonic() - started) * 1000))

    def health(self) -> ConnectorHealth:
        result = self.test_connection()
        return ConnectorHealth(
            status="CONNECTED" if result.ok else "DISCONNECTED", checked_at=result.checked_at,
            last_success_at=result.checked_at if result.ok else None,
            last_error_at=None if result.ok else result.checked_at,
            error_code=result.error_code, message=result.message, latency_ms=result.latency_ms,
        )


def fetch_events(base_url: str, token: str, start: str, end: str) -> tuple[list[CanonicalEvent], bool]:
    connector = ReferenceRESTConnector(base_url, token)
    events, cursor, seen = [], None, set()
    upstream_complete = True
    for _ in range(MAX_PAGES):
        if cursor is not None:
            if cursor in seen:
                raise ConnectorError("upstream repeated a pagination cursor")
            seen.add(cursor)
        page = connector.read_events(start, end, cursor=cursor, limit=PAGE_LIMIT)
        events.extend(page.items)
        upstream_complete = upstream_complete and page.complete
        cursor = page.next_cursor
        if cursor is None:
            return events, upstream_complete
    raise ConnectorError("upstream exceeded the configured page bound")


def canonical_batch(events: list[dict | CanonicalEvent], source_code: str, building_code: str,
                    connector_name: str = "reference-rest", connector_version: str = "0.1.0",
                    connector_status: Mapping[str, Any] | None = None) -> dict:
    records = []
    for item in events:
        if isinstance(item, CanonicalEvent):
            event_type = item.event_type
            event_id = item.external_event_id
            external_id = item.asset_external_id
            stamp = item.observed_at
            severity = item.severity
            details = item.details or {}
            source_metadata = dict(item.source_metadata or {})
        elif isinstance(item, dict):
            upstream_type = item.get("event_type")
            event_type = EVENT_TYPES.get(upstream_type)
            event_id = item.get("id")
            external_id = item.get("asset_external_id")
            stamp = item.get("observed_at")
            severity = item.get("severity", "INFO")
            details = item.get("details")
            if details is None:
                details = {}
            if not isinstance(details, dict):
                raise ConnectorError("source event details must be an object")
            source_metadata = {"upstream_event_type": upstream_type}
        else:
            raise ConnectorError("source event must be a canonical event or object")
        if event_type not in EVENT_TYPES.values():
            raise ConnectorError("upstream returned an unsupported event type; refusing a silently partial ingest")
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            raise ConnectorError("source event has invalid observed_at") from None
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ConnectorError("source event observed_at must be UTC")
        stamp = parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 128:
            raise ConnectorError("source event has invalid stable id")
        if not isinstance(external_id, str) or not 1 <= len(external_id) <= 128:
            raise ConnectorError("source event has invalid asset_external_id")
        records.append({"record_type": "event", "source_event_id": event_id,
                        "asset": {"external_id": external_id}, "event_type": event_type,
                        "observed_at": stamp, "severity": severity,
                        "details": details, "source_metadata": source_metadata})
    source = {"source_code": source_code,
              "connector": {"name": connector_name, "version": connector_version}}
    if connector_status is not None:
        source["connector_status"] = dict(connector_status)
    return {"contract_version": "1.0", "batch_id": str(uuid.uuid4()),
            "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "source": source,
            "building_code": building_code, "delivery_mode": "BACKFILL", "records": records}


def push_to_core(base_url: str, token: str, batch: dict, timeout: float = 15.0) -> dict:
    body = json.dumps(batch, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(base_url.rstrip("/") + "/api/v1/ingest/batches", data=body,
                                     headers={"Authorization": "Bearer " + token,
                                              "Content-Type": "application/json; charset=utf-8"}, method="POST")
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            if not retryable or attempt == MAX_RETRIES:
                raise ConnectorError(f"Core ingestion returned HTTP {exc.code}") from None
            delay = min(2 ** attempt, 8)
            try:
                delay = min(float(exc.headers.get("Retry-After", delay)), 8)
            except (TypeError, ValueError):
                pass
            time.sleep(max(0, delay))
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == MAX_RETRIES:
                raise ConnectorError("Core ingestion request failed after bounded retries") from None
            time.sleep(min(2 ** attempt, 8))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ConnectorError("Core returned invalid JSON") from None
    else:
        raise ConnectorError("Core ingestion request failed")
    if not isinstance(result, dict):
        raise ConnectorError("Core returned an invalid ingestion response")
    return result


def require_full_acceptance(result: dict) -> None:
    counts = result.get("counts")
    if not isinstance(counts, dict):
        raise ConnectorError("Core ingestion response omitted record counts")
    rejected = counts.get("rejected")
    if isinstance(rejected, bool) or not isinstance(rejected, int) or rejected < 0:
        raise ConnectorError("Core ingestion response contains invalid rejection counts")
    if rejected:
        batch_id = result.get("batch_id", "unknown")
        raise ConnectorError(f"Core rejected {rejected} record(s) in batch {batch_id}; source sync is incomplete")
    if result.get("status") not in {"ACCEPTED", "PARTIAL"}:
        raise ConnectorError("Core ingestion response has an unexpected batch status")


def _run_checkpointed_sync_locked(connector: ReferenceRESTConnector, store: SyncCheckpointStore,
                                  source_code: str, building_code: str, start: str, end: str,
                                  core_url: str, core_token: str, *, push=push_to_core) -> dict:
    """Fetch one page at a time and advance only after Core accepts its durable outbox batch."""
    _validate_utc_window(start, end)
    if not store.begin(source_code, building_code, start, end):
        state = store.state(source_code, building_code)
        return {"status": state["status"], "complete": True, "skipped": True,
                "page_count": state["page_count"], "core_results": []}

    results = []
    while True:
        state = store.state(source_code, building_code)
        batch = state["pending_batch"]
        if batch is None:
            cursor = state["next_cursor"]
            if state["page_count"] >= MAX_PAGES:
                raise ConnectorError("upstream exceeded the configured page bound")
            if cursor is not None and cursor in state["visited_cursors"]:
                raise ConnectorError("upstream repeated a pagination cursor")
            page = connector.read_events(start, end, cursor=cursor, limit=PAGE_LIMIT)
            batch = canonical_batch(
                list(page.items), source_code, building_code,
                connector_status={"upstream_page_complete": page.complete,
                                  "upstream_has_next_page": page.next_cursor is not None})
            store.save_pending(source_code, building_code, cursor, page.next_cursor,
                               page.complete, batch)

        # The persisted body and batch_id are reused verbatim after a timeout or
        # process crash, making Core's existing idempotent replay authoritative.
        result = push(core_url, core_token, batch)
        require_full_acceptance(result)
        results.append(result)
        advanced = store.advance_after_acceptance(source_code, building_code)
        if advanced["status"] != "RUNNING":
            return {"status": advanced["status"], "complete": advanced["complete"],
                    "skipped": False, "page_count": advanced["page_count"],
                    "core_results": results}


def run_checkpointed_sync(connector: ReferenceRESTConnector, store: SyncCheckpointStore,
                          source_code: str, building_code: str, start: str, end: str,
                          core_url: str, core_token: str, *, push=push_to_core) -> dict:
    with store.exclusive_run():
        return _run_checkpointed_sync_locked(connector, store, source_code, building_code,
                                             start, end, core_url, core_token, push=push)


def main():
    source_url = os.environ["REFERENCE_API_URL"]
    source_token = os.environ["REFERENCE_API_TOKEN"]
    core_url = os.environ["ANZMA_CORE_URL"]
    core_token = os.environ["ANZMA_CORE_API_KEY"]
    source_code = os.environ["ANZMA_SOURCE_CODE"]
    building_code = os.environ["ANZMA_BUILDING_CODE"]
    start = os.environ["ANZMA_FROM_UTC"]
    end = os.environ["ANZMA_TO_UTC"]
    _validate_url(source_url, "REFERENCE_API_URL")
    _validate_url(core_url, "ANZMA_CORE_URL")
    state_path = os.environ.get("ANZMA_CONNECTOR_STATE_PATH", "./state/reference-rest.sqlite3")
    result = run_checkpointed_sync(ReferenceRESTConnector(source_url, source_token),
                                  SyncCheckpointStore(state_path), source_code, building_code,
                                  start, end, core_url, core_token)
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] != "COMPLETE":
        raise ConnectorError("source sync finished with incomplete upstream coverage")


def _validate_url(value: str, label: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password \
            or parsed.query or parsed.fragment:
        raise ConnectorError(f"{label} must be an http(s) base URL without credentials, query, or fragment")
    if parsed.scheme != "https" and parsed.hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
        raise ConnectorError(f"{label} must use HTTPS outside loopback development")


def _validate_utc_window(start: str, end: str) -> None:
    parsed = []
    for value in (start, end):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            raise ConnectorError("event history bounds must be ISO-8601 UTC timestamps") from None
        if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
            raise ConnectorError("event history bounds must be timezone-aware UTC")
        parsed.append(stamp)
    if parsed[1] <= parsed[0]:
        raise ConnectorError("event history end must be later than start")


if __name__ == "__main__":
    main()
