"""Small authenticated, paginated REST reference source; run separately from Core."""
from __future__ import annotations

import hmac
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


EVENTS_PATH = Path(__file__).with_name("fixture") / "events.json"


class Handler(BaseHTTPRequestHandler):
    server_version = "AnzmaReferenceREST/1.0"

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            return self._json(200, {"status": "ok"})
        if parsed.path != "/v1/events":
            return self._json(404, {"error": {"code": "NOT_FOUND"}})
        expected = os.environ.get("REFERENCE_API_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        if not expected or not hmac.compare_digest(supplied, "Bearer " + expected):
            return self._json(401, {"error": {"code": "AUTH_INVALID"}})
        query = parse_qs(parsed.query, strict_parsing=False)
        try:
            limit = int(query.get("limit", ["2"])[0])
            offset = int(query.get("cursor", ["0"])[0])
        except ValueError:
            return self._json(400, {"error": {"code": "INVALID_PAGINATION"}})
        if not 1 <= limit <= 100 or offset < 0:
            return self._json(400, {"error": {"code": "INVALID_PAGINATION"}})
        start, end = query.get("from", [None])[0], query.get("to", [None])[0]
        if any(value is not None and not re.fullmatch(
                r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z", value)
               for value in (start, end)):
            return self._json(400, {"error": {"code": "INVALID_TIME_RANGE"}})
        try:
            start_dt = datetime.fromisoformat(start[:-1] + "+00:00") if start else None
            end_dt = datetime.fromisoformat(end[:-1] + "+00:00") if end else None
        except ValueError:
            return self._json(400, {"error": {"code": "INVALID_TIME_RANGE"}})
        if start_dt is not None and end_dt is not None and start_dt >= end_dt:
            return self._json(400, {"error": {"code": "INVALID_TIME_RANGE"}})
        try:
            events = json.loads(EVENTS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._json(503, {"error": {"code": "FIXTURE_UNAVAILABLE"}})
        filtered = []
        for event in events:
            try:
                observed = datetime.fromisoformat(event["observed_at"][:-1] + "+00:00")
            except (TypeError, ValueError, KeyError):
                return self._json(503, {"error": {"code": "INVALID_FIXTURE_TIMESTAMP"}})
            if (start_dt is None or observed >= start_dt) and (end_dt is None or observed < end_dt):
                filtered.append(event)
        page = filtered[offset:offset + limit]
        next_cursor = str(offset + len(page)) if offset + len(page) < len(filtered) else None
        # Completeness describes source coverage, while next_cursor only says
        # whether another page remains. These are independent signals.
        return self._json(200, {"items": page, "next_cursor": next_cursor,
                                "complete": True, "total": len(filtered)})

    def _json(self, status: int, value: dict):
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("reference-rest " + (fmt % args), flush=True)


def main():
    host = os.environ.get("REFERENCE_API_BIND", "127.0.0.1")
    port = int(os.environ.get("REFERENCE_API_PORT", "18111"))
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
