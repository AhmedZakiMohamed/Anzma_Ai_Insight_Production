"""envelope_evidence.py (Task 7): resolve Tool Envelope evidence references against PostgreSQL.

Scope: the *Tool Envelope* reference family, i.e. ``event_record`` and ``telemetry_record`` refs emitted by
analytical tools (``app/domain/tools/envelope.py::EvidenceRef``). The *alert*
JSON family (``{"source": "event", "record_id": 125}``) is verified by ``evidence.py::_verify`` and is NOT handled here.
See docs/EVIDENCE_VALIDATION.md.

What is checked, in this order, for one Envelope (or its ``to_dict()`` form):

1. Building scope is known: ``parameters["building"]`` (the tool's echo of the requested building code) is present.
2. Optional trusted hook: ``authorized_building_codes`` (server-side only; never derive it from model output).
3. Shape: every row's ``evidence_refs`` and every nested ``*_refs`` / ``evidence_refs`` list holds
   ``{"kind": str, "id": str}`` objects; supported kinds resolve to their corresponding fact table; ids are canonical positive BIGINTs;
   nested refs are a subset of the row's own ``evidence_refs`` (devices tool: sessions[], orphan_offline_end_refs,
   initial_state_evidence_refs); rows carry evidence; a row's ``building_code`` equals the scoped building.
4. Database (read-only, static SQL, bound parameters): every unique typed id exists in its fact table; its asset's
   building is the scoped building; ``observed_at`` and event type are compared when the reference carries them.

Asset equality is deliberately NOT required: compound rules such as SEC_BLIND_SPOT_ACCESS cite evidence from several
assets (a door reader and a camera) of the same building. Only the building is a scope boundary.

Every unique reference is looked up once however many times (or in however many nested lists) it is cited.
Nothing here writes to the database or builds SQL from model-supplied text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Collection, Iterable, Mapping

EVENT_RECORD_KIND = "event_record"
TELEMETRY_RECORD_KIND = "telemetry_record"
SUPPORTED_KINDS = frozenset({EVENT_RECORD_KIND, TELEMETRY_RECORD_KIND})
BUILDING_PARAMETER_KEYS = ("building", "building_code")
MAX_BIGINT = 2 ** 63 - 1
LOOKUP_CHUNK = 1000
MAX_LOCATIONS_PER_ISSUE = 10

_CANONICAL_ID = re.compile(r"[1-9][0-9]*", re.ASCII)


class EvidenceCode:
    """Stable machine-readable issue codes. Blocking issues make the result not ok."""
    BUILDING_SCOPE_MISSING = "BUILDING_SCOPE_MISSING"
    BUILDING_SCOPE_UNRESOLVED = "BUILDING_SCOPE_UNRESOLVED"
    BUILDING_NOT_AUTHORIZED = "BUILDING_NOT_AUTHORIZED"
    ROW_BUILDING_MISMATCH = "ROW_BUILDING_MISMATCH"
    MALFORMED_EVIDENCE_REF = "MALFORMED_EVIDENCE_REF"
    MALFORMED_EVIDENCE_ID = "MALFORMED_EVIDENCE_ID"
    UNSUPPORTED_EVIDENCE_KIND = "UNSUPPORTED_EVIDENCE_KIND"
    NESTED_REF_NOT_IN_ROW = "NESTED_REF_NOT_IN_ROW"
    ROW_WITHOUT_EVIDENCE = "ROW_WITHOUT_EVIDENCE"
    EVIDENCE_NOT_FOUND = "EVIDENCE_NOT_FOUND"
    EVIDENCE_BUILDING_MISMATCH = "EVIDENCE_BUILDING_MISMATCH"
    EVIDENCE_TIMESTAMP_MISMATCH = "EVIDENCE_TIMESTAMP_MISMATCH"
    EVIDENCE_EVENT_TYPE_MISMATCH = "EVIDENCE_EVENT_TYPE_MISMATCH"
    EVIDENCE_METRIC_MISMATCH = "EVIDENCE_METRIC_MISMATCH"
    EVIDENCE_VALUE_MISMATCH = "EVIDENCE_VALUE_MISMATCH"
    EVIDENCE_UNIT_MISMATCH = "EVIDENCE_UNIT_MISMATCH"
    EVIDENCE_QUALITY_MISMATCH = "EVIDENCE_QUALITY_MISMATCH"
    EVIDENCE_LOOKUP_FAILED = "EVIDENCE_LOOKUP_FAILED"
    EVIDENCE_DB_NOT_READ_ONLY = "EVIDENCE_DB_NOT_READ_ONLY"
    DUPLICATE_EVIDENCE_REF = "DUPLICATE_EVIDENCE_REF"          # non-blocking


# ------------------------------------------------------------------------------------------------------ static SQL
SQL_READ_ONLY = "SHOW transaction_read_only"

SQL_BUILDING = "SELECT building_id, building_code FROM buildings WHERE building_code = %(building_code)s"

SQL_EVENT_RECORDS = """
SELECT er.event_id, er.asset_id, er.ts, et.name AS event_type, a.asset_code, b.building_code
  FROM event_records er
  JOIN event_types et ON et.event_type_id = er.event_type_id
  JOIN assets a ON a.asset_id = er.asset_id
  JOIN locations l ON l.location_id = a.location_id
  JOIN buildings b ON b.building_id = l.building_id
 WHERE er.event_id = ANY(%(ids)s::bigint[])"""

SQL_TELEMETRY_RECORDS = """
SELECT tr.telemetry_id, tr.asset_id, tr.ts, tr.value, tr.quality, mt.name AS metric_code, mt.unit,
       a.asset_code, b.building_code
  FROM telemetry_records tr
  JOIN metric_types mt USING (metric_id)
  JOIN assets a USING (asset_id)
  JOIN locations l USING (location_id)
  JOIN buildings b ON b.building_id = l.building_id
 WHERE tr.telemetry_id = ANY(%(ids)s::bigint[])"""


# ------------------------------------------------------------------------------------------------------ result types
@dataclass(frozen=True)
class EvidenceIssue:
    code: str
    message: str
    kind: str | None = None
    id: str | None = None
    locations: tuple[str, ...] = ()
    blocking: bool = True

    def to_error(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message, "blocking": self.blocking}
        if self.kind is not None:
            out["kind"] = self.kind
        if self.id is not None:
            out["id"] = self.id
        if self.locations:
            out["locations"] = list(self.locations)
        return out


@dataclass(frozen=True)
class ResolvedEvidence:
    """One unique reference as it exists in the database."""
    kind: str
    id: str
    asset_id: int
    asset_code: str
    building_code: str
    ts: datetime
    event_type: str | None
    metric_code: str | None = None
    quality: str | None = None
    value: float | None = None
    unit: str | None = None


@dataclass(frozen=True)
class EnvelopeEvidenceResult:
    ok: bool
    expected_building: str | None
    unique_refs: int                                   # unique well-formed, supported references
    verified_refs: int                                 # of those, how many passed every check
    resolved: tuple[ResolvedEvidence, ...]
    issues: tuple[EvidenceIssue, ...]
    checks_run: tuple[str, ...]                        # only checks that actually executed
    verified_ids: frozenset[str] = field(default_factory=frozenset)
    verified_keys: frozenset[tuple[str, str]] = field(default_factory=frozenset)

    @property
    def blocking_issues(self) -> tuple[EvidenceIssue, ...]:
        return tuple(i for i in self.issues if i.blocking)

    @property
    def warnings(self) -> tuple[EvidenceIssue, ...]:
        return tuple(i for i in self.issues if not i.blocking)

    @property
    def errors(self) -> list[str]:
        return [i.to_error() for i in self.blocking_issues]

    def evidence_items(self) -> list[dict[str, Any]]:
        """Contract-style ``evidence`` list: one entry per unique resolved reference, DB values, flagged verified."""
        return [{"kind": r.kind, "id": r.id, "observed_at": r.ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                 **({"event_type": r.event_type} if r.event_type else {}),
                 **({"metric_code": r.metric_code} if r.metric_code else {}),
                 **({"value": r.value} if r.value is not None else {}),
                 **({"unit": r.unit} if r.unit else {}),
                 **({"quality": r.quality} if r.kind == TELEMETRY_RECORD_KIND else {}),
                 "asset_code": r.asset_code, "verified": (r.kind, r.id) in self.verified_keys}
                for r in sorted(self.resolved, key=lambda r: int(r.id))]

    @property
    def asset_ids(self) -> list[int]:
        return sorted({r.asset_id for r in self.resolved if (r.kind, r.id) in self.verified_keys})


# ------------------------------------------------------------------------------------------------------ structure
@dataclass
class _Occurrence:
    kind: str
    id: str
    location: str
    observed_at: str | None
    event_type: str | None
    metric_code: str | None
    value: Any
    unit: str | None
    quality: str | None
    row_index: int
    top_level: bool


def _is_ref_list_key(key: str) -> bool:
    return key == "evidence_refs" or key.endswith("_refs")


def parse_evidence_id(raw: Any) -> int | None:
    """Canonical fact id -> int, else None. Canonical = ASCII digits, no sign/space/leading zero, fits BIGINT."""
    if not isinstance(raw, str) or not _CANONICAL_ID.fullmatch(raw):
        return None
    value = int(raw)
    return value if value <= MAX_BIGINT else None


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _expected_building(parameters: Any) -> str | None:
    if not isinstance(parameters, Mapping):
        return None
    found = {parameters[k].strip() for k in BUILDING_PARAMETER_KEYS
             if isinstance(parameters.get(k), str) and parameters[k].strip()}
    return next(iter(found)) if len(found) == 1 else None      # two different values = ambiguous = unknown


def _collect(result: Mapping[str, Any], expected_building: str, require_row_evidence: bool):
    """Pure structural pass (no database). -> (occurrences, issues)."""
    issues: list[EvidenceIssue] = []
    occurrences: list[_Occurrence] = []
    rows = result.get("rows")
    if rows is None:
        return occurrences, issues
    if not isinstance(rows, list):
        return occurrences, [EvidenceIssue(EvidenceCode.MALFORMED_EVIDENCE_REF, "result.rows is not a list")]

    def read_ref(item: Any, location: str, row_index: int, top_level: bool):
        if not isinstance(item, Mapping) or not isinstance(item.get("kind"), str) or "id" not in item:
            issues.append(EvidenceIssue(EvidenceCode.MALFORMED_EVIDENCE_REF,
                                        "evidence reference must be an object with string 'kind' and 'id'",
                                        locations=(location,)))
            return None
        kind, raw_id = item["kind"], item["id"]
        if kind not in SUPPORTED_KINDS:
            issues.append(EvidenceIssue(EvidenceCode.UNSUPPORTED_EVIDENCE_KIND,
                                        f"evidence kind {kind!r} is not supported; it cannot be treated as verified",
                                        kind=kind, id=str(raw_id), locations=(location,)))
            return None
        if parse_evidence_id(raw_id) is None:
            issues.append(EvidenceIssue(EvidenceCode.MALFORMED_EVIDENCE_ID,
                                        f"evidence id {raw_id!r} is not a canonical positive integer string",
                                        kind=kind, id=str(raw_id), locations=(location,)))
            return None
        observed = item.get("observed_at")
        etype = item.get("event_type")
        metric = item.get("metric_code")
        value = item.get("value")
        unit = item.get("unit")
        quality = item.get("quality")
        return _Occurrence(kind, raw_id, location, observed if isinstance(observed, str) else None,
                           etype if isinstance(etype, str) else None,
                           metric if isinstance(metric, str) else None, value,
                           unit if isinstance(unit, str) else None,
                           quality if isinstance(quality, str) else None,
                           row_index, top_level)

    def walk_nested(value: Any, path: str, row_index: int, top_keys: set, row_base: str):
        if isinstance(value, Mapping):
            for key, child in value.items():
                child_path = f"{path}.{key}"
                if key == "evidence_refs" and path == row_base:
                    continue                                    # the row's own list, handled separately
                if _is_ref_list_key(str(key)) and isinstance(child, list):
                    seen_local: set = set()
                    for n, item in enumerate(child):
                        occ = read_ref(item, f"{child_path}[{n}]", row_index, False)
                        if occ is None:
                            continue
                        ident = (occ.kind, occ.id)
                        if ident not in top_keys:
                            issues.append(EvidenceIssue(
                                EvidenceCode.NESTED_REF_NOT_IN_ROW,
                                "nested evidence reference is not among the row's own evidence_refs",
                                kind=occ.kind, id=occ.id, locations=(occ.location,)))
                        if ident in seen_local:
                            issues.append(EvidenceIssue(EvidenceCode.DUPLICATE_EVIDENCE_REF,
                                                        "evidence reference repeated within one list",
                                                        kind=occ.kind, id=occ.id, locations=(occ.location,), blocking=False))
                        seen_local.add(ident)
                        occurrences.append(occ)
                else:
                    walk_nested(child, child_path, row_index, top_keys, row_base)
        elif isinstance(value, list):
            for n, child in enumerate(value):
                walk_nested(child, f"{path}[{n}]", row_index, top_keys, row_base)

    global_seen: dict[tuple, str] = {}
    for i, row in enumerate(rows):
        base = f"rows[{i}]"
        if not isinstance(row, Mapping):
            issues.append(EvidenceIssue(EvidenceCode.MALFORMED_EVIDENCE_REF, "row is not an object", locations=(base,)))
            continue
        row_building = row.get("building_code")
        if isinstance(row_building, str) and row_building != expected_building:
            issues.append(EvidenceIssue(EvidenceCode.ROW_BUILDING_MISMATCH,
                                        "row building_code differs from the building the result is scoped to",
                                        locations=(base,)))
        refs = row.get("evidence_refs")
        top_keys: set = set()
        if refs is None or refs == []:
            if require_row_evidence:
                issues.append(EvidenceIssue(EvidenceCode.ROW_WITHOUT_EVIDENCE,
                                            "row carries no evidence references, so its values are unsupported",
                                            locations=(base,)))
        elif not isinstance(refs, list):
            issues.append(EvidenceIssue(EvidenceCode.MALFORMED_EVIDENCE_REF, "evidence_refs is not a list",
                                        locations=(f"{base}.evidence_refs",)))
        else:
            for n, item in enumerate(refs):
                occ = read_ref(item, f"{base}.evidence_refs[{n}]", i, True)
                if occ is None:
                    continue
                ident = (occ.kind, occ.id)
                if ident in top_keys:
                    issues.append(EvidenceIssue(EvidenceCode.DUPLICATE_EVIDENCE_REF,
                                                "evidence reference repeated within one row (resolved once)",
                                                kind=occ.kind, id=occ.id, locations=(occ.location,), blocking=False))
                elif ident in global_seen:
                    issues.append(EvidenceIssue(EvidenceCode.DUPLICATE_EVIDENCE_REF,
                                                f"evidence reference also cited by {global_seen[ident]} (resolved once)",
                                                kind=occ.kind, id=occ.id, locations=(occ.location,), blocking=False))
                top_keys.add(ident)
                global_seen.setdefault(ident, base)
                occurrences.append(occ)
        walk_nested(row, base, i, top_keys, base)
    return occurrences, issues


# ------------------------------------------------------------------------------------------------------ database
def _read_only(cur) -> bool:
    cur.execute(SQL_READ_ONLY)
    row = cur.fetchone()
    value = row["transaction_read_only"] if isinstance(row, Mapping) else row[0]
    return str(value).lower() == "on"


def _chunks(items: list[int], size: int) -> Iterable[list[int]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _merge_issues(issues: list[EvidenceIssue]) -> list[EvidenceIssue]:
    """One issue per (code, kind, id): the first message, every citing location (capped)."""
    merged: dict[tuple, EvidenceIssue] = {}
    for issue in issues:
        key = (issue.code, issue.kind, issue.id, issue.blocking, issue.message if issue.id is None else "")
        if key not in merged:
            merged[key] = issue
        else:
            known = merged[key]
            locations = tuple(dict.fromkeys(known.locations + issue.locations))[:MAX_LOCATIONS_PER_ISSUE]
            merged[key] = EvidenceIssue(known.code, known.message, known.kind, known.id, locations, known.blocking)
    return list(merged.values())


def validate_envelope_evidence(cur, result: Any, *, authorized_building_codes: Collection[str] | None = None,
                               require_read_only: bool = True,
                               require_row_evidence: bool = True) -> EnvelopeEvidenceResult:
    """Validate every evidence reference of one Tool Envelope. Never raises for bad *data*; fails closed.

    cur: dict-row cursor. With ``require_read_only`` (default) the transaction must report
         ``transaction_read_only = on``, otherwise no lookup is made (``EVIDENCE_DB_NOT_READ_ONLY``).
         Transaction handling (rollback after an error) stays with the caller, as in the existing tools.
    result: ``Envelope`` or its ``to_dict()`` output.
    authorized_building_codes: TRUSTED server-side context only (``None`` = no extra restriction, which is the
         honest state today: the application has one deployment bearer token and no per-user ACL).
    """
    if cur is None:
        raise ValueError("a read-only cursor is required to resolve evidence")
    data = result.to_dict() if hasattr(result, "to_dict") else result
    if not isinstance(data, Mapping):
        raise TypeError("result must be an Envelope or a mapping")

    checks: list[str] = []
    building = _expected_building(data.get("parameters"))
    if building is None:
        return EnvelopeEvidenceResult(False, None, 0, 0, (), (EvidenceIssue(
            EvidenceCode.BUILDING_SCOPE_MISSING,
            "the result has no unambiguous building in its parameters, so evidence cannot be scoped"),), ())

    if authorized_building_codes is not None:
        checks.append("evidence_building_authorized")
        if building not in {str(code) for code in authorized_building_codes}:
            return EnvelopeEvidenceResult(False, building, 0, 0, (), (EvidenceIssue(
                EvidenceCode.BUILDING_NOT_AUTHORIZED,
                "the result's building is not in the trusted authorized building set"),), tuple(checks))

    occurrences, structural = _collect(data, building, require_row_evidence)
    checks += ["evidence_ref_well_formed", "evidence_nested_refs_consistent"]

    by_id: dict[tuple[str, str], list[_Occurrence]] = {}
    for occ in occurrences:
        by_id.setdefault((occ.kind, occ.id), []).append(occ)
    unique_ids = sorted(by_id, key=lambda key: (key[0], int(key[1])))
    issues: list[EvidenceIssue] = list(structural)
    resolved: dict[tuple[str, str], ResolvedEvidence] = {}
    bad_ids: set[tuple[str, str]] = set()

    if unique_ids:
        try:
            if require_read_only and not _read_only(cur):
                issues.append(EvidenceIssue(EvidenceCode.EVIDENCE_DB_NOT_READ_ONLY,
                                            "evidence is resolved only inside a read-only transaction"))
                return _finish(building, unique_ids, resolved, bad_ids, issues, checks)
            cur.execute(SQL_BUILDING, {"building_code": building})
            if cur.fetchone() is None:
                issues.append(EvidenceIssue(EvidenceCode.BUILDING_SCOPE_UNRESOLVED,
                                            "the result's building does not exist"))
                return _finish(building, unique_ids, resolved, bad_ids, issues, checks)
            found: dict[tuple[str, int], Mapping[str, Any]] = {}
            for kind in sorted({key[0] for key in unique_ids}):
                ids = [int(ident) for ref_kind, ident in unique_ids if ref_kind == kind]
                query = SQL_EVENT_RECORDS if kind == EVENT_RECORD_KIND else SQL_TELEMETRY_RECORDS
                column = "event_id" if kind == EVENT_RECORD_KIND else "telemetry_id"
                for chunk in _chunks(ids, LOOKUP_CHUNK):
                    cur.execute(query, {"ids": chunk})
                    for row in cur.fetchall():
                        found[(kind, int(row[column]))] = row
        except Exception as exc:                                  # driver errors differ; fail closed, never leak details
            issues.append(EvidenceIssue(EvidenceCode.EVIDENCE_LOOKUP_FAILED,
                                        f"evidence lookup failed ({type(exc).__name__}); nothing is treated as verified"))
            return _finish(building, unique_ids, {}, set(unique_ids), issues, checks)

        checks += ["evidence_row_exists", "evidence_building_scope"]
        for key in unique_ids:
            kind, ident = key
            row = found.get((kind, int(ident)))
            first = by_id[key][0]
            where = tuple(o.location for o in by_id[key])[:MAX_LOCATIONS_PER_ISSUE]
            if row is None:
                bad_ids.add(key)
                issues.append(EvidenceIssue(EvidenceCode.EVIDENCE_NOT_FOUND,
                                            f"referenced {kind} row does not exist (deleted or never stored)",
                                            kind=first.kind, id=ident, locations=where))
                continue
            if row["building_code"] != building:
                bad_ids.add(key)
                issues.append(EvidenceIssue(EvidenceCode.EVIDENCE_BUILDING_MISMATCH,
                                            "referenced record belongs to a different building than the result's scope",
                                            kind=first.kind, id=ident, locations=where))
                continue
            stored_ts, bad = row["ts"], False
            for occ in by_id[key]:
                if occ.observed_at is not None:
                    claimed = _parse_ts(occ.observed_at)
                    if claimed is None or claimed != stored_ts:
                        bad = True
                        issues.append(EvidenceIssue(EvidenceCode.EVIDENCE_TIMESTAMP_MISMATCH,
                                                    "observed_at differs from the stored record",
                                                    kind=occ.kind, id=ident, locations=(occ.location,)))
                if kind == EVENT_RECORD_KIND and occ.event_type is not None and occ.event_type != row["event_type"]:
                    bad = True
                    issues.append(EvidenceIssue(EvidenceCode.EVIDENCE_EVENT_TYPE_MISMATCH,
                                                "event_type differs from the stored record",
                                                kind=occ.kind, id=ident, locations=(occ.location,)))
                if kind == TELEMETRY_RECORD_KIND:
                    fields = (
                        (occ.metric_code, row["metric_code"], EvidenceCode.EVIDENCE_METRIC_MISMATCH,
                         "metric_code differs from the stored record"),
                        (occ.unit, row["unit"], EvidenceCode.EVIDENCE_UNIT_MISMATCH,
                         "unit differs from the stored record"),
                        (occ.quality, row["quality"], EvidenceCode.EVIDENCE_QUALITY_MISMATCH,
                         "quality differs from the stored record"),
                    )
                    for supplied, stored, code, message in fields:
                        if supplied is not None and supplied != stored:
                            bad = True
                            issues.append(EvidenceIssue(code, message, kind=occ.kind, id=ident,
                                                        locations=(occ.location,)))
                    if occ.value is not None:
                        try:
                            value_matches = (not isinstance(occ.value, bool)
                                             and Decimal(str(occ.value)) == Decimal(str(row["value"])))
                        except (InvalidOperation, ValueError):
                            value_matches = False
                        if not value_matches:
                            bad = True
                            issues.append(EvidenceIssue(EvidenceCode.EVIDENCE_VALUE_MISMATCH,
                                                        "value differs from the stored record",
                                                        kind=occ.kind, id=ident, locations=(occ.location,)))
            if bad:
                bad_ids.add(key)
                continue
            resolved[key] = ResolvedEvidence(first.kind, ident, int(row["asset_id"]), row["asset_code"],
                                             row["building_code"], stored_ts, row.get("event_type"),
                                             row.get("metric_code"), row.get("quality"),
                                             float(row["value"]) if kind == TELEMETRY_RECORD_KIND else None,
                                             row.get("unit"))
    return _finish(building, unique_ids, resolved, bad_ids, issues, checks)


def _finish(building: str, unique_ids: list[tuple[str, str]],
            resolved: dict[tuple[str, str], ResolvedEvidence], bad_ids: set[tuple[str, str]],
            issues: list[EvidenceIssue], checks: list[str]) -> EnvelopeEvidenceResult:
    merged = _merge_issues(issues)
    # a reference cited at a structurally invalid place (e.g. nested but not in the row) is not "verified"
    flagged = {(i.kind, i.id) for i in merged if i.blocking and i.id is not None}
    verified_keys = frozenset(key for key, r in resolved.items() if key not in flagged and key not in bad_ids)
    verified = frozenset(ident for kind, ident in verified_keys if kind == EVENT_RECORD_KIND)
    ok = not any(i.blocking for i in merged)
    return EnvelopeEvidenceResult(ok, building, len(unique_ids), len(verified_keys), tuple(resolved.values()),
                                  tuple(merged), tuple(checks), verified, verified_keys)
