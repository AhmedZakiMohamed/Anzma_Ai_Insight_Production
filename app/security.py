"""Replaceable bearer authentication for normal Core REST and Socket.IO access."""

from __future__ import annotations

import hmac
import logging
import os
from http.cookies import SimpleCookie
from typing import Protocol
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware


class Authenticator(Protocol):
    def authenticate(self, token: str | None) -> bool: ...


class EnvironmentBearerAuthenticator:
    """Compare a deployment-provided bearer secret without logging or storing it."""

    def authenticate(self, token: str | None) -> bool:
        expected = os.getenv("ANZMA_API_TOKEN", "")
        return bool(len(expected) >= 32 and token and hmac.compare_digest(expected, token))


authenticator: Authenticator = EnvironmentBearerAuthenticator()


def cors_origins_from_env(environ=None, log: logging.Logger | None = None) -> list[str]:
    """Return exact valid HTTP(S) origins; invalid entries are ignored with a warning."""
    values = os.environ if environ is None else environ
    sink = logging.getLogger("anzma") if log is None else log
    result = []
    for raw in values.get("ANZMA_CORS_ALLOWED_ORIGINS", "").split(","):
        origin = raw.strip()
        if not origin:
            continue
        try:
            parsed = urlsplit(origin)
            valid = ("*" not in origin and parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                     and parsed.username is None and parsed.password is None and parsed.path == ""
                     and not parsed.query and not parsed.fragment and "?" not in origin and "#" not in origin
                     and not any(ch.isspace() for ch in origin) and not parsed.netloc.endswith(":"))
            port = parsed.port
            if port is not None and not 1 <= port <= 65535:
                valid = False
            if valid:
                hostname = parsed.hostname.lower()
                host = f"[{hostname}]" if ":" in hostname else hostname
                if port is not None and port != (80 if parsed.scheme == "http" else 443):
                    host += f":{port}"
                origin = f"{parsed.scheme.lower()}://{host}"
        except ValueError:
            valid = False
        if not valid:
            sink.warning("Ignoring invalid origin in ANZMA_CORS_ALLOWED_ORIGINS.")
            continue
        if origin not in result:
            result.append(origin)
    return result


def configure_cors(app, origins: list[str]) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


def socketio_cors_origins(origins: list[str]):
    """Engine.IO None means same-origin validation; explicit lists are exact allowlists."""
    return origins if origins else None


def token_from_authorization(value: str | None) -> str | None:
    if not value:
        return None
    scheme, separator, token = value.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    return token.strip() or None


def require_api_auth(request: Request) -> None:
    """FastAPI dependency for ordinary user/control API routes."""
    if os.getenv("ANZMA_AUTH_DISABLED", "false").strip().lower() in {"1", "true", "yes", "on"}:
        return
    authorization = request.headers.get("authorization")
    token = token_from_authorization(authorization)
    if not authorization:
        raise HTTPException(401, detail={"code": "AUTH_MISSING", "message": "Bearer credentials are required."},
                            headers={"WWW-Authenticate": "Bearer"})
    if not authenticator.authenticate(token):
        raise HTTPException(401, detail={"code": "AUTH_INVALID", "message": "Bearer credentials are invalid."},
                            headers={"WWW-Authenticate": "Bearer"})


def socket_authenticate(auth: dict | None, cookie_header: str | None = None) -> bool:
    if os.getenv("ANZMA_AUTH_DISABLED", "false").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    token = auth.get("token") if isinstance(auth, dict) else None
    if not token and cookie_header:
        try:
            cookie = SimpleCookie()
            cookie.load(cookie_header)
            token = cookie.get("anzma_session").value if cookie.get("anzma_session") else None
        except Exception:
            token = None
    return authenticator.authenticate(token if isinstance(token, str) else None)
