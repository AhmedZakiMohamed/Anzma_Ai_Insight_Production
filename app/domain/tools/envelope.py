"""Single result envelope for deterministic analytics tools (ADR-0001 §1, D-12, open question g).

Pure Python: no database, FastAPI or model dependency. Tools compute; the envelope only *describes* what was
computed, how complete it is and which evidence supports each row. Nothing here guesses a value: a missing
`data_as_of` stays ``None`` and an unknown quality stays ``None``.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Mapping, Sequence

ENVELOPE_FIELDS = (
    "result_id", "tool", "parameters", "data_as_of", "generated_at", "source_ids", "complete", "quality",
    "truncated", "columns", "rows", "total_rows", "limitations", "query_version", "trace_id",
)
QUALITY_ORDER = ("GOOD", "UNCERTAIN", "BAD")  # INGESTION_CONTRACT §7; worst wins when aggregating


class ResultStatus(str, Enum):
    """ADR-0001 open question g (Proposed enum, closed here so code and fixtures share one spelling)."""
    COMPLETE = "COMPLETE"              # everything retrieved and validated; zero rows is a legitimate answer
    PARTIAL = "PARTIAL"                # supported, but data missing or a page failed
    UNSUPPORTED = "UNSUPPORTED"        # the source does not declare the capability (e.g. history.events)
    CANNOT_COMPUTE = "CANNOT_COMPUTE"  # D-03 timezone / validation failures


class LimitationCode(str, Enum):
    """Machine-readable limitation reasons. Values are stable strings: fixtures and UIs may match on them."""
    TIMEZONE_UNAVAILABLE = "TIMEZONE_UNAVAILABLE"            # D-03 / G-TZ-01
    INVALID_PARAMETER = "INVALID_PARAMETER"                  # D-03 / G-TZ-02
    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"        # D-12 / G-COMP-02
    PAGE_FAILED = "PAGE_FAILED"                              # D-12 / G-COMP-01
    COMPLETION_NOT_CONFIRMED = "COMPLETION_NOT_CONFIRMED"    # D-12 / G-COMP-04
    ROWS_TRUNCATED = "ROWS_TRUNCATED"                        # D-12
    UNTRUSTED_TIMESTAMP_EXCLUDED = "UNTRUSTED_TIMESTAMP_EXCLUDED"  # D-11 / G-TS-02
    MISSING_EVENT_IDENTITY = "MISSING_EVENT_IDENTITY"        # D-04 / G-DUP-02
    CORRELATION_UNAVAILABLE = "CORRELATION_UNAVAILABLE"      # D-01 / G-DOOR-03: no correlation identifier in contract 1.0
    TIMEZONE_PROVENANCE_UNCONFIRMED = "TIMEZONE_PROVENANCE_UNCONFIRMED"  # migration 009 backfill value, D-03 spirit
    INITIAL_STATE_UNKNOWN = "INITIAL_STATE_UNKNOWN"          # D-08 / G-OFF-09
    FLAPPING_NOT_EVALUATED = "FLAPPING_NOT_EVALUATED"        # D-07
    OPEN_OUTAGE_LOWER_BOUND = "OPEN_OUTAGE_LOWER_BOUND"      # D-09
    EARLY_HOURS_NOT_COVERED = "EARLY_HOURS_NOT_COVERED"      # open question a
    ORPHAN_OFFLINE_END = "ORPHAN_OFFLINE_END"                # D-06 / G-OFF-04: recovery event with no known start
    SYSTEM_NOT_ELIGIBLE_FOR_OFFLINE = "SYSTEM_NOT_ELIGIBLE_FOR_OFFLINE"  # D-05 / G-OFF-01: fault-type or unmapped system
    CATALOGUE_NOT_MIGRATED = "CATALOGUE_NOT_MIGRATED"        # migration 014 (event_class / outage_class) not applied
    DATA_AS_OF_UNKNOWN = "DATA_AS_OF_UNKNOWN"


@dataclass(frozen=True)
class Limitation:
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "code", LimitationCode(self.code).value)  # unknown codes fail loudly
        if not self.message:
            raise ValueError("a limitation needs a human-readable message")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            out["details"] = jsonable(self.details)
        return out


@dataclass(frozen=True)
class EvidenceRef:
    """Pointer to one stored fact behind a row. ``kind`` + ``id`` is the stable key; extras are descriptive."""
    kind: str
    id: str
    observed_at: datetime | None = None
    event_type: str | None = None
    metric_code: str | None = None
    value: float | int | None = None
    unit: str | None = None
    quality: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "id": str(self.id)}
        if self.observed_at is not None:
            out["observed_at"] = iso_utc(self.observed_at)
        if self.event_type:
            out["event_type"] = self.event_type
        if self.metric_code:
            out["metric_code"] = self.metric_code
        if self.value is not None:
            out["value"] = self.value
        if self.unit:
            out["unit"] = self.unit
        if self.quality:
            out["quality"] = self.quality
        return out


@dataclass(frozen=True)
class Row:
    values: Mapping[str, Any]
    evidence_refs: tuple[EvidenceRef, ...] = ()

    def to_dict(self, columns: Sequence[str]) -> dict[str, Any]:
        extra = set(self.values) - set(columns)
        if extra:
            raise ValueError(f"row has values outside declared columns: {sorted(extra)}")
        out = {c: jsonable(self.values.get(c)) for c in columns}
        out["evidence_refs"] = [ref.to_dict() for ref in self.evidence_refs]
        return out


@dataclass(frozen=True)
class Envelope:
    result_id: str
    tool: str
    parameters: Mapping[str, Any]
    data_as_of: datetime | None
    generated_at: datetime
    source_ids: tuple[str, ...]
    status: ResultStatus
    quality: str | None
    truncated: bool
    columns: tuple[str, ...]
    rows: tuple[Row, ...]
    total_rows: int
    limitations: tuple[Limitation, ...]
    query_version: str
    trace_id: str

    @property
    def complete(self) -> bool:
        return self.status is ResultStatus.COMPLETE and not self.truncated

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict. Key order is fixed (``ENVELOPE_FIELDS`` then ``status``) for stable snapshots."""
        return {
            "result_id": self.result_id, "tool": self.tool, "parameters": jsonable(self.parameters),
            "data_as_of": iso_utc(self.data_as_of) if self.data_as_of else None,
            "generated_at": iso_utc(self.generated_at), "source_ids": list(self.source_ids),
            "complete": self.complete, "quality": self.quality, "truncated": self.truncated,
            "columns": list(self.columns), "rows": [row.to_dict(self.columns) for row in self.rows],
            "total_rows": self.total_rows, "limitations": [lim.to_dict() for lim in self.limitations],
            "query_version": self.query_version, "trace_id": self.trace_id, "status": self.status.value,
        }


# ---------------------------------------------------------------------------------------------- helpers
def iso_utc(value: datetime) -> str:
    """UTC ISO-8601 with an explicit offset. Naive datetimes are rejected: D-03 forbids guessing a zone."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime rejected: an explicit UTC offset is required (ADR-0001 D-03)")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def jsonable(value: Any) -> Any:
    """Deterministic JSON-safe conversion (sorted mapping keys, ISO datetimes, Decimal as string)."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, (date, timedelta)):
        return value.isoformat() if isinstance(value, date) else value.total_seconds()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): jsonable(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
        return [jsonable(v) for v in items]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"value of type {type(value).__name__} is not JSON-representable")


def canonical_json(value: Any) -> str:
    return json.dumps(jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_result_id(tool: str, parameters: Mapping[str, Any], data_as_of: datetime | None, query_version: str) -> str:
    """Deterministic id: same tool + parameters + data_as_of + version => same id (golden tests, caching).

    ``generated_at`` is deliberately excluded so re-running unchanged data reproduces the id.
    """
    payload = canonical_json({"tool": tool, "parameters": parameters,
                              "data_as_of": iso_utc(data_as_of) if data_as_of else None,
                              "query_version": query_version})
    return "res_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def worst_quality(values: Sequence[str | None]) -> str | None:
    known = [v for v in values if v in QUALITY_ORDER]
    return max(known, key=QUALITY_ORDER.index) if known else None


# ---------------------------------------------------------------------------------------------- builder
@dataclass
class EnvelopeBuilder:
    """Collects a tool's outputs and applies the D-12 completeness rules in one place.

    Tools do not set ``complete`` themselves; they declare facts (status, limitations, truncation) and the
    builder derives the rest, so chat, reports and dashboards cannot disagree about completeness.
    """
    tool: str
    query_version: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    data_as_of: datetime | None = None
    source_ids: Sequence[str] = ()
    columns: Sequence[str] = ()
    rows: Sequence[Row] = ()
    limitations: Sequence[Limitation] = ()
    status: ResultStatus = ResultStatus.COMPLETE
    quality: str | None = None
    max_rows: int | None = None
    trace_id: str | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    id_factory: Callable[[], str] = lambda: str(uuid.uuid4())

    def build(self) -> Envelope:
        status = ResultStatus(self.status)
        limitations = list(self.limitations)
        rows = list(self.rows)
        total_rows = len(rows)
        truncated = False

        if self.max_rows is not None:
            if self.max_rows < 0:
                raise ValueError("max_rows must be >= 0")
            if total_rows > self.max_rows:
                rows, truncated = rows[: self.max_rows], True
                limitations.append(Limitation(LimitationCode.ROWS_TRUNCATED.value,
                                              f"Showing {self.max_rows} of {total_rows} rows.",
                                              {"shown": self.max_rows, "total_rows": total_rows}))
        if status in (ResultStatus.UNSUPPORTED, ResultStatus.CANNOT_COMPUTE):
            if rows:
                raise ValueError(f"{status.value} results must not carry rows (never present zero as unsupported)")
            total_rows = 0
        if status is not ResultStatus.COMPLETE and not limitations:
            raise ValueError(f"{status.value} results must explain themselves with at least one limitation")
        if self.data_as_of is None and status in (ResultStatus.COMPLETE, ResultStatus.PARTIAL):
            limitations.append(Limitation(LimitationCode.DATA_AS_OF_UNKNOWN.value,
                                          "No reliable data timestamp was available for this result."))
        if self.quality is not None and self.quality not in QUALITY_ORDER:
            raise ValueError(f"quality must be one of {QUALITY_ORDER}")
        for row in rows:
            row.to_dict(self.columns)  # validates every row against the declared columns now, not at render time

        return Envelope(
            result_id=compute_result_id(self.tool, self.parameters, self.data_as_of, self.query_version),
            tool=self.tool, parameters=dict(self.parameters), data_as_of=self.data_as_of,
            generated_at=self.clock(), source_ids=tuple(sorted(set(self.source_ids))), status=status,
            quality=self.quality, truncated=truncated, columns=tuple(self.columns), rows=tuple(rows),
            total_rows=total_rows, limitations=tuple(limitations), query_version=self.query_version,
            trace_id=self.trace_id or self.id_factory(),
        )


def cannot_compute(*, tool: str, query_version: str, parameters: Mapping[str, Any], limitation: Limitation,
                   **kwargs: Any) -> Envelope:
    """Convenience for D-03 style failures: no rows, explicit reason (G-TZ-01 / G-TZ-02)."""
    return EnvelopeBuilder(tool=tool, query_version=query_version, parameters=parameters,
                           status=ResultStatus.CANNOT_COMPUTE, limitations=(limitation,), **kwargs).build()


def unsupported(*, tool: str, query_version: str, parameters: Mapping[str, Any], limitation: Limitation,
                **kwargs: Any) -> Envelope:
    """D-12: the source does not declare the capability. Distinct from a complete zero result (G-COMP-02)."""
    return EnvelopeBuilder(tool=tool, query_version=query_version, parameters=parameters,
                           status=ResultStatus.UNSUPPORTED, limitations=(limitation,), **kwargs).build()
