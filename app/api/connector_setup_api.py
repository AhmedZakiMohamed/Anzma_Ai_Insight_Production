"""Narrow, reference-only guided connection check for the G1 setup flow."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from time import monotonic
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from connectors.reference_rest.connector import ReferenceRESTConnector


class ConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_id: str = Field(min_length=1, max_length=80)
    endpoint_url: str = Field(min_length=8, max_length=512)
    bearer_token: SecretStr


MAX_REQUEST_BYTES = 8192


def _approved_origin(endpoint: str) -> str:
    try:
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or
                parsed.path not in {"", "/"} or parsed.query or parsed.fragment or
                parsed.netloc.endswith(":") or
                any(ord(ch) < 32 or ord(ch) == 127 for ch in endpoint)):
            raise ValueError
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        origin = f"{parsed.scheme.lower()}://{host}"
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            origin += f":{port}"
    except ValueError:
        raise HTTPException(422, detail={"code": "ENDPOINT_INVALID",
                                          "message": "Enter a valid HTTP(S) origin without a path or embedded credentials."}) from None

    approved = {item.strip().rstrip("/") for item in
                os.getenv("ANZMA_CONNECTOR_ALLOWED_ORIGINS", "").split(",") if item.strip()}
    if origin not in approved:
        raise HTTPException(422, detail={"code": "ENDPOINT_NOT_APPROVED",
                                          "message": "This endpoint is not approved by the system operator."})
    http_approved = {item.strip().rstrip("/") for item in
                     os.getenv("ANZMA_CONNECTOR_ALLOW_HTTP_ORIGINS", "").split(",") if item.strip()}
    if parsed.scheme == "http" and origin not in http_approved:
        raise HTTPException(422, detail={"code": "HTTPS_REQUIRED",
                                          "message": "The system operator has not approved HTTP for this endpoint."})
    return origin


def create_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/v1/connectors/test-connection", summary="Test an approved reference connector")
    async def test_connection(request: Request):
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > MAX_REQUEST_BYTES:
                raise HTTPException(413, detail={"code": "REQUEST_TOO_LARGE",
                                                  "message": "Connection-check request exceeds the size limit."})
            payload.extend(chunk)
        try:
            body = ConnectionRequest.model_validate_json(bytes(payload))
        except ValidationError:
            # Pydantic errors can contain submitted values; never return them for a
            # request that includes a credential.
            raise HTTPException(422, detail={"code": "REQUEST_INVALID",
                                              "message": "Provide a connector ID, approved endpoint origin, and bearer credential."}) from None
        if body.connector_id != "anzma.reference-rest":
            raise HTTPException(422, detail={"code": "CONNECTOR_UNAVAILABLE",
                                              "message": "Only the synthetic reference connector is available in this setup preview."})
        credential = body.bearer_token.get_secret_value()
        if not credential or len(credential) > 4096 or any(
                ord(char) < 32 or ord(char) == 127 for char in credential):
            raise HTTPException(422, detail={"code": "CREDENTIAL_INVALID",
                                              "message": "The credential is empty, too long, or contains unsupported characters."})
        origin = _approved_origin(body.endpoint_url)
        started = monotonic()
        checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        result = ReferenceRESTConnector(origin, credential).test_connection()
        # Do not return the supplied endpoint or credentials, and keep source failures sanitized.
        message = result.message if result.ok else "The endpoint did not pass the reference history/read-access check."
        return {"connector_id": body.connector_id, "ok": result.ok, "checked_at": checked_at,
                "latency_ms": max(0, int((monotonic() - started) * 1000)),
                "capabilities": list(result.capabilities),
                "error_code": None if result.ok else (result.error_code or "CONNECTION_FAILED"),
                "message": message}

    return router
