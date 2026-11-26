"""Small capability-based connector interface shared by vendor adapters.

The connector runs outside the Core process. Implement only the capability
protocols advertised by the manifest; a missing capability is different from
an implemented capability that returned an empty page.
"""
from __future__ import annotations

import re
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Generic, Mapping, Protocol, TypeVar, runtime_checkable


def _unique_strings(name: str, values: tuple[str, ...]) -> None:
    if not isinstance(values, tuple) or any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"{name} must be a tuple of non-empty strings")
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")


@dataclass(frozen=True)
class ConnectorManifest:
    id: str
    version: str
    api_contract: str
    vendor: str
    supported_domains: tuple[str, ...]
    capabilities: tuple[str, ...]
    authentication_modes: tuple[str, ...]
    pagination_style: str
    freshness_model: str
    minimum_core_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,127}", self.id):
            raise ValueError("id must be a lowercase stable connector identifier")
        for name in ("version", "minimum_core_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
                raise ValueError(f"{name} must use MAJOR.MINOR.PATCH")
        for name in ("api_contract", "vendor", "pagination_style", "freshness_model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.pagination_style not in {"cursor", "offset", "none"}:
            raise ValueError("pagination_style must be cursor, offset, or none")
        for name in ("supported_domains", "capabilities", "authentication_modes"):
            _unique_strings(name, getattr(self, name))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ConnectorManifest":
        if not isinstance(value, Mapping):
            raise ValueError("connector manifest must be an object")
        expected = set(cls.__dataclass_fields__)
        missing, extra = expected - set(value), set(value) - expected
        if missing or extra:
            raise ValueError(f"manifest fields mismatch; missing={sorted(missing)}, extra={sorted(extra)}")
        normalized = dict(value)
        for name in ("supported_domains", "capabilities", "authentication_modes"):
            items = normalized[name]
            if not isinstance(items, (list, tuple)):
                raise ValueError(f"{name} must be an array")
            normalized[name] = tuple(items)
        return cls(**normalized)


def load_manifest(path: str | Path) -> ConnectorManifest:
    """Read and validate a connector's JSON manifest before it is registered."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read connector manifest: {type(exc).__name__}") from None
    return ConnectorManifest.from_mapping(value)


T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    """One page of results; `complete` describes source coverage, not pagination end."""
    items: tuple[T, ...]
    next_cursor: str | None
    complete: bool

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple):
            raise ValueError("items must be an immutable tuple")
        if self.next_cursor is not None and (not isinstance(self.next_cursor, str) or not self.next_cursor):
            raise ValueError("next_cursor must be a non-empty string or null")
        if not isinstance(self.complete, bool):
            raise ValueError("complete must be a boolean")


@dataclass(frozen=True)
class ConnectionTest:
    ok: bool
    checked_at: str
    capabilities: tuple[str, ...]
    latency_ms: int | None = None
    error_code: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class ConnectorHealth:
    status: str
    checked_at: str
    last_success_at: str | None = None
    last_error_at: str | None = None
    error_code: str | None = None
    message: str | None = None
    latency_ms: int | None = None

    def __post_init__(self) -> None:
        if self.status not in {"CONNECTED", "DEGRADED", "DISCONNECTED", "UNKNOWN"}:
            raise ValueError("status must be CONNECTED, DEGRADED, DISCONNECTED, or UNKNOWN")


@dataclass(frozen=True)
class ExternalAsset:
    external_id: str
    asset_type: str
    name: str | None = None
    parent_external_id: str | None = None
    location: str | None = None
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ExternalPoint:
    external_id: str
    asset_external_id: str
    data_type: str
    name: str | None = None
    unit: str | None = None
    metadata: Mapping[str, Any] | None = None


def _require_utc(value: str, field_name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from None
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")


@dataclass(frozen=True)
class CanonicalEvent:
    external_event_id: str
    asset_external_id: str
    event_type: str
    observed_at: str
    quality: str = "UNKNOWN"
    received_at: str | None = None
    severity: str = "INFO"
    details: Mapping[str, Any] | None = None
    source_metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for name in ("external_event_id", "asset_external_id", "event_type"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be a non-empty string")
        for name in ("quality", "severity"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be a non-empty string")
        for name in ("details", "source_metadata"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Mapping):
                raise ValueError(f"{name} must be an object")
        _require_utc(self.observed_at, "observed_at")
        if self.received_at is not None:
            _require_utc(self.received_at, "received_at")


@dataclass(frozen=True)
class TelemetryReading:
    point_external_id: str
    observed_at: str
    value: int | float | str | bool | None
    unit: str | None = None
    quality: str = "UNKNOWN"
    received_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.point_external_id, str) or not self.point_external_id.strip():
            raise ValueError("point_external_id must be a non-empty string")
        _require_utc(self.observed_at, "observed_at")
        if self.received_at is not None:
            _require_utc(self.received_at, "received_at")


@dataclass(frozen=True)
class StateObservation:
    asset_external_id: str
    state: str
    observed_at: str
    quality: str = "UNKNOWN"

    def __post_init__(self) -> None:
        if not self.asset_external_id or not self.state:
            raise ValueError("asset_external_id and state are required")
        _require_utc(self.observed_at, "observed_at")


@runtime_checkable
class ConnectorLifecycle(Protocol):
    manifest: ConnectorManifest

    def test_connection(self) -> ConnectionTest: ...
    def health(self) -> ConnectorHealth: ...


@runtime_checkable
class AssetDiscoveryConnector(Protocol):
    def discover_assets(self, *, cursor: str | None = None, limit: int = 100) -> Page[ExternalAsset]: ...
    def discover_points(self, asset_external_id: str, *, cursor: str | None = None,
                        limit: int = 100) -> Page[ExternalPoint]: ...


@runtime_checkable
class EventHistoryConnector(Protocol):
    def read_events(self, from_utc: str, to_utc: str, *, cursor: str | None = None,
                    limit: int = 100, filters: Mapping[str, Any] | None = None) -> Page[CanonicalEvent]: ...


@runtime_checkable
class TelemetryHistoryConnector(Protocol):
    def read_telemetry(self, from_utc: str, to_utc: str, *, cursor: str | None = None,
                       limit: int = 100, filters: Mapping[str, Any] | None = None) -> Page[TelemetryReading]: ...


@runtime_checkable
class CurrentStateConnector(Protocol):
    def read_current_state(self, asset_external_ids: tuple[str, ...]) -> tuple[StateObservation, ...]: ...


@runtime_checkable
class EventSubscriptionConnector(Protocol):
    def subscribe_events(self, checkpoint: str | None = None) -> AsyncIterator[CanonicalEvent]: ...
