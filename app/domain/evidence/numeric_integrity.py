"""numeric_integrity.py (Task 7): the deterministic analytical result is authoritative; text must agree with it.

Pure Python (no database, no model). Three jobs:

1. FINGERPRINT. ``result_fingerprint(result)`` = ``sha256:`` + SHA-256 of a canonical JSON form of the result.
   Contributing fields (``FINGERPRINT_FIELDS``): result_id, tool, query_version, status, parameters, data_as_of,
   source_ids, complete, quality, truncated, columns, rows (values AND evidence refs, nested refs included),
   total_rows, limitations (code, message, details). A policy number is hashed too, so a future change to this list
   changes every fingerprint visibly. EXCLUDED: ``generated_at`` and ``trace_id`` (a clock reading and a random uuid
   that do not change what was computed). Canonical form: JSON-safe via ``envelope.jsonable``, mapping keys sorted,
   integral floats written as ints, NaN/Infinity rejected, no whitespace. Order that carries meaning is kept (rows =
   ranking, sessions = chronology, columns). Order that does not is normalised: object keys, evidence-ref lists
   (keys ``evidence_refs`` / ``*_refs``, treated as sets), ``source_ids`` and ``limitations`` (treated as sets).

2. CLAIM VALIDATION. ``validate_numeric_claims(text, result)`` extracts numeric claims from free text and compares
   each with the *authoritative metric fields* of the result (never with "any digit anywhere in the evidence"):
   counts, durations (seconds; explicit seconds->minutes/hours/days conversion rounded half-up to the claim's own
   precision), percentages vs fractions, measurements with a declared unit, row/total counts, column sums, per-row
   evidence counts, ranking positions, result ids and evidence ids named in the text. Not claims (ignored):
   identifiers and codes that mix letters and digits, entity names taken from the result, dates/times (checked
   separately against the result's own timestamps), version strings, citations like ``[E3]``, list numbering.

3. FALLBACK. ``deterministic_fallback(result)`` renders a summary from the original values only. It is checked by
   the same validator in tests, so a fallback can never contradict the result.

Known limits (also in docs/EVIDENCE_VALIDATION.md): entity attribution is per sentence/line; composite durations
("2 h 5 min"), averages, differences, and negations ("no outages") other than "no <noun>" are not understood and
are rejected or ignored rather than guessed; the fallback is English only.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from app.domain.tools.envelope import jsonable

# ------------------------------------------------------------------------------------------------ fingerprint
FINGERPRINT_POLICY = 1
FINGERPRINT_FIELDS = ("result_id", "tool", "query_version", "status", "parameters", "data_as_of", "source_ids",
                      "complete", "quality", "truncated", "columns", "rows", "total_rows", "limitations")
FINGERPRINT_EXCLUDED = ("generated_at", "trace_id")


class NumericIssueCode:
    UNSUPPORTED_NUMBER = "UNSUPPORTED_NUMBER"                  # invented, or changed with nothing to compare it to
    VALUE_MODIFIED = "VALUE_MODIFIED"                          # named entity + compatible metric exist, number differs
    ENTITY_VALUE_MISMATCH = "ENTITY_VALUE_MISMATCH"            # value exists, but for a different entity
    WRONG_UNIT = "WRONG_UNIT"
    PERCENT_FRACTION_MISMATCH = "PERCENT_FRACTION_MISMATCH"
    RANK_MISMATCH = "RANK_MISMATCH"
    RANK_OUT_OF_RANGE = "RANK_OUT_OF_RANGE"
    RESULT_ID_MISMATCH = "RESULT_ID_MISMATCH"
    EVIDENCE_REF_NOT_IN_RESULT = "EVIDENCE_REF_NOT_IN_RESULT"
    TEMPORAL_VALUE_NOT_IN_RESULT = "TEMPORAL_VALUE_NOT_IN_RESULT"
    RESULT_FINGERPRINT_MISMATCH = "RESULT_FINGERPRINT_MISMATCH"
    UNIT_OMITTED = "UNIT_OMITTED"                              # non-blocking
    ZERO_NOT_CONFIRMED = "ZERO_NOT_CONFIRMED"                  # non-blocking


def as_result_dict(result: Any) -> dict[str, Any]:
    """Envelope / SealedResult / mapping -> a JSON-safe *copy*. The argument is never modified."""
    if isinstance(result, SealedResult):
        return result.result
    data = result.to_dict() if hasattr(result, "to_dict") else result
    if not isinstance(data, Mapping):
        raise TypeError("result must be an Envelope, a SealedResult or a mapping")
    return copy.deepcopy(jsonable(data))


_REF_SORT = lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canon(value: Any, key: str | None = None) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("NaN/Infinity cannot be fingerprinted")
        return int(value) if value.is_integer() else value
    if isinstance(value, Mapping):
        return {str(k): _canon(value[k], str(k)) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        items = [_canon(v) for v in value]
        if key is not None and (key == "evidence_refs" or key.endswith("_refs")):
            items.sort(key=_REF_SORT)
        return items
    raise TypeError(f"{type(value).__name__} is not canonicalisable")


def canonical_payload(result: Any) -> dict[str, Any]:
    data = as_result_dict(result)
    payload: dict[str, Any] = {"policy": FINGERPRINT_POLICY}
    for name in FINGERPRINT_FIELDS:
        value = data.get(name)
        if name == "source_ids" and isinstance(value, list):
            value = sorted(value, key=str)
        if name == "limitations" and isinstance(value, list):
            value = sorted((_canon(v) for v in value), key=_REF_SORT)
        payload[name] = _canon(value, name)
    return payload


def canonical_json(result: Any) -> str:
    return json.dumps(canonical_payload(result), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def result_fingerprint(result: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(result).encode("utf-8")).hexdigest()


def verify_fingerprint(result: Any, expected: str) -> bool:
    try:
        return result_fingerprint(result) == expected
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class SealedResult:
    """Immutable snapshot of an authoritative result. ``result`` returns a fresh copy on every access, so mutating
    what a caller (or a model-facing layer) holds cannot change what was sealed."""
    fingerprint: str
    result_id: str
    _json: str

    @property
    def result(self) -> dict[str, Any]:
        return json.loads(self._json)


def seal_result(result: Any) -> SealedResult:
    """Call right after the tool returns, BEFORE anything model-facing touches the result."""
    data = as_result_dict(result)
    return SealedResult(result_fingerprint(data), str(data.get("result_id")),
                        json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False))


# ------------------------------------------------------------------------------------------------ metric kinds
COUNT, DURATION_S, PERCENT, FRACTION, MEASUREMENT, NUMBER = "count", "duration_s", "percent", "fraction", "measurement", "number"


@dataclass(frozen=True)
class MetricSpec:
    kind: str
    unit: str | None = None            # MEASUREMENT only: canonical unit text, e.g. "°C", "kW", "events/h"


_METRICS: dict[str, dict[str, MetricSpec]] = {
    "doors.after_hours": {"opening_count": MetricSpec(COUNT), "uncounted_door_forced_events": MetricSpec(COUNT),
                          "window_count": MetricSpec(COUNT)},
    "devices.offline_frequency": {
        "outages_started_in_period": MetricSpec(COUNT), "outages_ongoing_at_period_start": MetricSpec(COUNT),
        "downtime_seconds_in_period": MetricSpec(DURATION_S), "orphan_offline_end_count": MetricSpec(COUNT),
        "duplicate_events_ignored": MetricSpec(COUNT), "duration_seconds": MetricSpec(DURATION_S),
        "overlap_seconds": MetricSpec(DURATION_S), "repeated_start_events": MetricSpec(COUNT)},
}
_GLOBAL_KEYS = {"total_rows": MetricSpec(COUNT)}
_NON_METRIC_KEY = re.compile(r"(?:^|_)(?:id|ids|version)$|^trace_id$")
_ENTITY_KEYS = ("asset_code", "asset_name", "asset_external_id", "door_external_id")


def register_metric_specs(tool: str, specs: Mapping[str, MetricSpec]) -> None:
    """Future tools declare which numeric columns are authoritative metrics (and their kind/unit)."""
    _METRICS.setdefault(tool, {}).update(specs)


def _spec_for(tool: str, key: str) -> MetricSpec | None:
    """None = not a metric (an identifier); otherwise the declared, inferred or generic (NUMBER) spec."""
    declared = _METRICS.get(tool, {}).get(key) or _GLOBAL_KEYS.get(key)
    if declared:
        return declared
    if _NON_METRIC_KEY.search(key):
        return None
    if key.endswith("_seconds"):
        return MetricSpec(DURATION_S)
    if key.endswith(("_pct", "_percent")):
        return MetricSpec(PERCENT)
    if key.endswith(("_fraction", "_ratio")):
        return MetricSpec(FRACTION)
    if key.endswith("_count") or key in ("count", "total"):
        return MetricSpec(COUNT)
    return MetricSpec(NUMBER)


@dataclass(frozen=True)
class Value:
    value: Decimal
    kind: str
    unit: str | None
    key: str
    path: str
    row: int | None                    # None = global (not attached to one entity)


def _dec(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _numeric(value: Any, declared: bool) -> Decimal | None:
    if isinstance(value, str):
        return _dec(value) if declared else None          # Decimal values are serialised as strings
    return _dec(value) if isinstance(value, (int, float)) else None


@dataclass
class Pool:
    values: list[Value] = field(default_factory=list)
    entities: list[tuple[str, ...]] = field(default_factory=list)     # per row: lowercase names
    evidence_ids: set[str] = field(default_factory=set)
    temporal: set[str] = field(default_factory=set)
    known_strings: list[str] = field(default_factory=list)
    row_count: int = 0
    status: str | None = None


_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")


def _split_names(value: Any) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()] if isinstance(value, str) else []


def build_pool(result: Mapping[str, Any]) -> Pool:
    tool = str(result.get("tool"))
    pool = Pool(status=result.get("status"))

    def add(value, kind, unit, key, path, row):
        pool.values.append(Value(value, kind, unit, key, path, row))

    def walk(node, path, row, key):
        if isinstance(node, Mapping):
            for k, child in node.items():
                if k == "evidence_refs":
                    for ref in child if isinstance(child, list) else []:
                        if isinstance(ref, Mapping) and isinstance(ref.get("id"), str):
                            pool.evidence_ids.add(ref["id"])
                    continue
                if str(k).endswith("_refs") and isinstance(child, list):
                    continue
                walk(child, f"{path}.{k}", row, str(k))
        elif isinstance(node, list):
            for n, child in enumerate(node):
                walk(child, f"{path}[{n}]", row, key)
        elif key:
            spec = _spec_for(tool, key)
            if spec is None:
                return
            number = _numeric(node, declared=spec.kind != NUMBER)
            if number is not None:
                add(number, spec.kind, spec.unit, key, path, row)

    walk(result.get("parameters"), "parameters", None, "")
    for n, lim in enumerate(result.get("limitations") or []):
        if isinstance(lim, Mapping):
            walk(lim.get("details"), f"limitations[{n}].details", None, "")
    total = _dec(result.get("total_rows"))
    if total is not None:
        add(total, COUNT, None, "total_rows", "total_rows", None)

    rows = result.get("rows") if isinstance(result.get("rows"), list) else []
    pool.row_count = len(rows)
    add(Decimal(len(rows)), COUNT, None, "row_count", "rows", None)
    sums: dict[str, Decimal] = {}
    for i, row in enumerate(rows):
        if not isinstance(row, Mapping):
            pool.entities.append(())
            continue
        names = []
        for key in _ENTITY_KEYS:
            names += _split_names(row.get(key))
        pool.entities.append(tuple(dict.fromkeys(n.lower() for n in names)))
        walk({k: v for k, v in row.items() if k not in _ENTITY_KEYS}, f"rows[{i}]", i, "")
        refs = row.get("evidence_refs")
        if isinstance(refs, list):
            add(Decimal(len(refs)), COUNT, None, "evidence_ref_count", f"rows[{i}].evidence_refs", i)
            for ref in refs:
                if isinstance(ref, Mapping) and isinstance(ref.get("id"), str):
                    pool.evidence_ids.add(ref["id"])
        if isinstance(row.get("sessions"), list):
            add(Decimal(len(row["sessions"])), COUNT, None, "session_count", f"rows[{i}].sessions", i)
        for key, raw in row.items():
            spec = _spec_for(tool, key)
            if spec and spec.kind in (COUNT, DURATION_S):
                number = _numeric(raw, declared=True)
                if number is not None:
                    sums[key] = sums.get(key, Decimal(0)) + number
    for key, total_value in sums.items():
        add(total_value, _spec_for(tool, key).kind, None, f"sum({key})", f"rows.*.{key}", None)

    params = result.get("parameters") if isinstance(result.get("parameters"), Mapping) else {}
    start, end = _iso(params.get("start_utc")), _iso(params.get("end_utc_exclusive"))
    if start is not None and end is not None and end > start:
        add(Decimal(str((end - start).total_seconds())).normalize(), DURATION_S, None, "period_seconds", "parameters", None)

    def strings(node, key=None):
        if isinstance(node, str):
            if key not in ("generated_at", "trace_id"):
                yield node
        elif isinstance(node, Mapping):
            for k, v in node.items():
                yield from strings(v, str(k))
        elif isinstance(node, list):
            for v in node:
                yield from strings(v, key)

    for text in strings({k: v for k, v in result.items() if k not in FINGERPRINT_EXCLUDED}):
        for d in _DATE.findall(text):
            pool.temporal.add(d)
        for t in _TIME.findall(text):
            pool.temporal.add(_norm_time(t))
            pool.temporal.add(_norm_time(t)[:5])
        if len(text) >= 3 and any(ch.isdigit() for ch in text):
            pool.known_strings.append(text)
    pool.known_strings.sort(key=len, reverse=True)
    return pool


def _iso(value: Any):
    from datetime import datetime
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _norm_time(t: str) -> str:
    parts = t.split(":")
    return ":".join([parts[0].zfill(2)] + parts[1:])


# ------------------------------------------------------------------------------------------------ claim extraction
_DIGIT_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")
_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20}
_UNIT_CLASS = {"s": "seconds", "sec": "seconds", "secs": "seconds", "second": "seconds", "seconds": "seconds",
               "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes",
               "h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours",
               "d": "days", "day": "days", "days": "days",
               "%": "percent", "percent": "percent", "pct": "percent"}
_MEASURE_UNITS = {"°c": "°C", "celsius": "°C", "°f": "°F", "kw": "kW", "kwh": "kWh", "w": "W", "v": "V",
                  "mbps": "Mbps", "gbps": "Gbps", "ms": "ms", "ppm": "ppm", "psi": "psi", "rh": "%RH"}
_COUNT_NOUNS = r"(?:openings?|outages?|events?|sessions?|doors?|devices?|cameras?|alerts?|incidents?|records?|rows?|times|alarms?|links?)"

_RESULT_ID = re.compile(r"\bres_[0-9A-Za-z]+\b")
_EVIDENCE = re.compile(r"\bevent[_ ]records?\s*[:#]?\s*(\d+(?:\s*(?:,|and|&)\s*\d+)*)", re.I)
_CITATION = re.compile(r"\[\s*E\d+(?:\s*[,\-–]\s*E?\d+)*\s*\]")
_DATETIME = re.compile(r"\b(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)(?:Z|[+-]\d{2}:?\d{2})?)?\b")
_TIME_TOKEN = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_VERSION = re.compile(r"\bv\d+(?:\.\d+)*\b|\b\d+\.\d+\.\d+(?:\.\d+)*\b", re.I)
_RANK = re.compile(r"\brank(?:ed|ing)?\s*(?:#|no\.?|number|of)?\s*(\d+)\b|#(\d+)\b|\b(\d+)(?:st|nd|rd|th)\b", re.I)
_NO_NOUN = re.compile(rf"\bno\s+(?:[A-Za-z-]+\s+)?{_COUNT_NOUNS}\b", re.I)
_WORD_NUM = re.compile(rf"\b({'|'.join(_WORDS)})\s+(?:more\s+)?(?={_COUNT_NOUNS}\b|(?:seconds?|minutes?|hours?|days?)\b)", re.I)
_ATTACHED = re.compile(r"(?<![\w.])(-?\d+(?:\.\d+)?)(%|(?:s|sec|secs|min|mins|h|hr|hrs|d)\b)", re.I)
_IDENT_TOKEN = re.compile(r"\b(?=[A-Za-z0-9_\-]*[A-Za-z])(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]+\b")
_LIST_NUMBER = re.compile(r"(?m)^\s*\d+[.)]\s")
_NUMBER = re.compile(r"(?<![\w.])(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)"
                     r"(?:\s?(?P<unit>%|percent\b|per\s?cent\b|pct\b|[A-Za-z°µ]+(?:/[A-Za-z]+)?))?")
_SEGMENT_SPLIT = re.compile(r"\n+|(?<=[.!?])\s+|;\s+")


@dataclass(frozen=True)
class Claim:
    value: Decimal
    decimals: int
    unit_class: str                   # none | seconds | minutes | hours | days | percent | measure | noun
    unit: str | None                  # canonical measurement unit when unit_class == "measure"
    raw: str
    segment: str


def _decimals(text: str) -> int:
    exp = Decimal(text).as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


def _classify_unit(token: str | None):
    """-> (unit_class, canonical measurement unit or None)."""
    if not token:
        return "none", None
    low = re.sub(r"\s+", "", token.lower())
    if low in ("%", "percent", "percent", "pct"):
        return "percent", None
    if low in _UNIT_CLASS:
        return _UNIT_CLASS[low], None
    if low in _MEASURE_UNITS:
        return "measure", _MEASURE_UNITS[low]
    if "/" in low:
        return "measure", low
    return "noun", None


@dataclass
class Extraction:
    claims: list[Claim] = field(default_factory=list)
    ranks: list[tuple[int, str, str]] = field(default_factory=list)        # (n, raw, segment)
    result_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
    times: list[str] = field(default_factory=list)


def extract_claims(text: str, known_strings: Sequence[str] = ()) -> Extraction:
    out = Extraction()
    text = (text or "").translate(_DIGIT_MAP)
    for segment in _SEGMENT_SPLIT.split(text):
        if not segment.strip():
            continue
        seg = segment
        for known in known_strings:
            seg = re.sub(re.escape(known), lambda m: " " * len(m.group(0)), seg, flags=re.I)

        def take(pattern, handler, s):
            for m in list(pattern.finditer(s)):
                handler(m)
            return pattern.sub(lambda m: " " * len(m.group(0)), s)

        seg = take(_RESULT_ID, lambda m: out.result_ids.append(m.group(0)), seg)
        seg = take(_EVIDENCE, lambda m: out.evidence_ids.extend(re.findall(r"\d+", m.group(1))), seg)
        seg = _CITATION.sub(lambda m: " " * len(m.group(0)), seg)

        def dt(m):
            out.dates.append(m.group(1))
            if m.group(2):
                out.times.append(_norm_time(m.group(2)))
        seg = take(_DATETIME, dt, seg)
        seg = take(_TIME_TOKEN, lambda m: out.times.append(_norm_time(m.group(0))), seg)
        seg = _VERSION.sub(lambda m: " " * len(m.group(0)), seg)
        seg = _LIST_NUMBER.sub(lambda m: " " * len(m.group(0)), seg)

        def rank(m):
            n = next(g for g in m.groups() if g)
            out.ranks.append((int(n), m.group(0), segment))
        seg = take(_RANK, rank, seg)

        def zero(m):
            out.claims.append(Claim(Decimal(0), 0, "noun", None, m.group(0), segment))
        seg = take(_NO_NOUN, zero, seg)

        def word(m):
            out.claims.append(Claim(Decimal(_WORDS[m.group(1).lower()]), 0, "noun", None, m.group(0).strip(), segment))
        seg = take(_WORD_NUM, word, seg)

        def attached(m):
            cls, unit = _classify_unit(m.group(2))
            out.claims.append(Claim(Decimal(m.group(1)), _decimals(m.group(1)), cls, unit, m.group(0), segment))
        seg = take(_ATTACHED, attached, seg)
        seg = _IDENT_TOKEN.sub(lambda m: " " * len(m.group(0)), seg)

        for m in _NUMBER.finditer(seg):
            number = m.group(1).replace(",", "")
            cls, unit = _classify_unit(m.group("unit"))
            out.claims.append(Claim(Decimal(number), _decimals(number), cls, unit, m.group(0).strip(), segment))
    return out


# ------------------------------------------------------------------------------------------------ matching
def _round(x: Decimal, places: int) -> Decimal:
    try:
        return x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return x


_TIME_DIVISOR = {"seconds": Decimal(1), "minutes": Decimal(60), "hours": Decimal(3600), "days": Decimal(86400)}


def _match(c: Claim, v: Value) -> str | None:
    """'exact' | 'unit_omitted' | None. Conversions: seconds -> minutes/hours/days and fraction -> percent only."""
    cv, dp, u = c.value, c.decimals, c.unit_class
    rounds_to = lambda x: _round(x, dp) == cv
    if v.kind == COUNT:
        return "exact" if u in ("none", "noun") and v.value == cv else None
    if v.kind == DURATION_S:
        if u in _TIME_DIVISOR:
            return "exact" if rounds_to(v.value / _TIME_DIVISOR[u]) else None
        return "unit_omitted" if u == "none" and v.value == cv else None
    if v.kind == PERCENT:
        if u == "percent":
            return "exact" if rounds_to(v.value) else None
        return "unit_omitted" if u == "none" and v.value == cv else None
    if v.kind == FRACTION:
        if u == "percent":
            return "exact" if rounds_to(v.value * 100) else None
        return "exact" if u == "none" and rounds_to(v.value) else None
    if v.kind == MEASUREMENT:
        if u == "measure" and c.unit == v.unit:
            return "exact" if rounds_to(v.value) else None
        return "unit_omitted" if u == "none" and v.value == cv else None
    return "exact" if u in ("none", "noun") and v.value == cv else None      # NUMBER: exact value, no unit semantics


def _compatible(c: Claim, v: Value) -> bool:
    table = {COUNT: ("none", "noun"), DURATION_S: ("none", "seconds", "minutes", "hours", "days"),
             PERCENT: ("none", "percent"), FRACTION: ("none", "percent"), MEASUREMENT: ("none", "measure"),
             NUMBER: ("none", "noun")}
    return c.unit_class in table[v.kind]


def _scale_confusion(c: Claim, v: Value) -> bool:
    cv, dp = c.value, c.decimals
    if v.kind == FRACTION:
        return (c.unit_class == "percent" and _round(v.value, dp) == cv) or \
               (c.unit_class == "none" and _round(v.value * 100, dp) == cv and v.value != 0)
    if v.kind == PERCENT:
        return (c.unit_class in ("none", "percent") and _round(v.value / 100, dp) == cv and v.value != 0) or \
               (c.unit_class == "percent" and _round(v.value * 100, dp) == cv and v.value != 0)
    return False


def _fmt(x: Decimal) -> str:
    return format(x.normalize(), "f") if x != x.to_integral() else str(int(x))


@dataclass(frozen=True)
class NumericIssue:
    code: str
    message: str
    text: str | None = None
    blocking: bool = True

    def to_error(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "text": self.text, "blocking": self.blocking}


@dataclass(frozen=True)
class NumericValidation:
    ok: bool
    issues: tuple[NumericIssue, ...]
    claims_checked: int
    checks_run: tuple[str, ...]

    @property
    def blocking_issues(self) -> tuple[NumericIssue, ...]:
        return tuple(i for i in self.issues if i.blocking)

    @property
    def warnings(self) -> tuple[NumericIssue, ...]:
        return tuple(i for i in self.issues if not i.blocking)

    @property
    def errors(self) -> list[str]:
        return [i.to_error() for i in self.blocking_issues]


def _mentioned_rows(segment: str, pool: Pool) -> set[int]:
    low = segment.lower()
    return {i for i, names in enumerate(pool.entities)
            if any(re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", low) for n in names)}


def validate_numeric_claims(text: str, result: Any, *, check_temporal: bool = True) -> NumericValidation:
    data = as_result_dict(result)
    pool = build_pool(data)
    ex = extract_claims(text, pool.known_strings)
    issues: list[NumericIssue] = []

    for rid in dict.fromkeys(ex.result_ids):
        if rid != data.get("result_id"):
            issues.append(NumericIssue(NumericIssueCode.RESULT_ID_MISMATCH,
                                       "the text names a result id that is not this result's id", rid))
    for eid in dict.fromkeys(ex.evidence_ids):
        if eid not in pool.evidence_ids:
            issues.append(NumericIssue(NumericIssueCode.EVIDENCE_REF_NOT_IN_RESULT,
                                       f"evidence id {eid} is not among the result's evidence references", eid))

    for n, raw, segment in ex.ranks:
        mentioned = _mentioned_rows(segment, pool)
        if n < 1 or n > pool.row_count:
            issues.append(NumericIssue(NumericIssueCode.RANK_OUT_OF_RANGE,
                                       f"rank {n} is outside the {pool.row_count} ranked row(s)", raw))
        elif mentioned and (n - 1) not in mentioned:
            issues.append(NumericIssue(NumericIssueCode.RANK_MISMATCH,
                                       f"the named entity does not hold rank {n} in the result order", raw))

    for c in ex.claims:
        mentioned = _mentioned_rows(c.segment, pool)
        scoped = [v for v in pool.values if v.row is None or v.row in mentioned] if mentioned else pool.values
        outcomes = [(v, _match(c, v)) for v in scoped]
        hits = [(v, o) for v, o in outcomes if o]
        if hits:
            exact = [h for h in hits if h[1] == "exact"]
            if not exact:
                issues.append(NumericIssue(NumericIssueCode.UNIT_OMITTED,
                                           f"{_fmt(c.value)} matches an authoritative value but states no unit",
                                           c.raw, blocking=False))
            if c.value == 0 and pool.status != "COMPLETE" and all(v.key in ("total_rows", "row_count") for v, _ in hits):
                issues.append(NumericIssue(NumericIssueCode.ZERO_NOT_CONFIRMED,
                                           "zero rows were returned but the result is not COMPLETE, so zero is not a "
                                           "confirmed finding", c.raw, blocking=False))
            continue
        everything = [v for v in pool.values if _match(c, v)]
        if everything:
            issues.append(NumericIssue(NumericIssueCode.ENTITY_VALUE_MISMATCH,
                                       f"{_fmt(c.value)} is an authoritative value, but not of the entity this sentence names",
                                       c.raw))
            continue
        if any(_scale_confusion(c, v) for v in pool.values):
            issues.append(NumericIssue(NumericIssueCode.PERCENT_FRACTION_MISMATCH,
                                       f"{c.raw!r} confuses a percentage with a fraction of an authoritative value", c.raw))
            continue
        if any(v.value == c.value for v in pool.values):      # same number, but no unit reading of it matched
            issues.append(NumericIssue(NumericIssueCode.WRONG_UNIT,
                                       f"{c.raw!r} equals an authoritative value but with a different unit", c.raw))
            continue
        local = [v for v in scoped if v.row is not None and _compatible(c, v)] if mentioned else []
        if local:
            expected = ", ".join(sorted({_fmt(v.value) for v in local})[:6])
            issues.append(NumericIssue(NumericIssueCode.VALUE_MODIFIED,
                                       f"{_fmt(c.value)} differs from the authoritative value(s) for the named entity ({expected})",
                                       c.raw))
            continue
        issues.append(NumericIssue(NumericIssueCode.UNSUPPORTED_NUMBER,
                                   f"{_fmt(c.value)} is not an authoritative value of this result", c.raw))

    checks = ["numeric_claims_authoritative", "result_id_matches", "evidence_mentions_in_result"]
    if check_temporal:
        checks.append("temporal_values_in_result")
        for d in dict.fromkeys(ex.dates):
            if d not in pool.temporal:
                issues.append(NumericIssue(NumericIssueCode.TEMPORAL_VALUE_NOT_IN_RESULT,
                                           f"date {d} does not occur in the result", d))
        for t in dict.fromkeys(ex.times):
            if t not in pool.temporal and t[:5] not in pool.temporal:
                issues.append(NumericIssue(NumericIssueCode.TEMPORAL_VALUE_NOT_IN_RESULT,
                                           f"time {t} does not occur in the result", t))
    ok = not any(i.blocking for i in issues)
    return NumericValidation(ok, tuple(issues), len(ex.claims) + len(ex.ranks), tuple(checks))


# ------------------------------------------------------------------------------------------------ fallback
def _num_text(value: Any) -> str:
    d = _dec(value)
    return "not available" if d is None else _fmt(d)


def _refs_text(row: Mapping[str, Any], limit: int = 5) -> str:
    ids = [r["id"] for r in row.get("evidence_refs") or [] if isinstance(r, Mapping) and isinstance(r.get("id"), str)]
    if not ids:
        return ""
    shown = ", ".join(ids[:limit])
    return f" Evidence: event_record:{shown}" + (" and others." if len(ids) > limit else ".")


def _doors_line(row: Mapping[str, Any]) -> str:
    n = _num_text(row.get("opening_count"))
    forced = row.get("uncounted_door_forced_events")
    extra = f"; {_num_text(forced)} door_forced alarms were not counted as openings" if forced else ""
    return f"- {row.get('asset_name')} ({row.get('asset_code')}): {n} door openings after hours{extra}.{_refs_text(row)}"


def _devices_line(row: Mapping[str, Any]) -> str:
    started = _num_text(row.get("outages_started_in_period"))
    down = _num_text(row.get("downtime_seconds_in_period"))
    text = (f"- {row.get('asset_name')} ({row.get('asset_code')}): {started} outages started in the period; "
            f"{down} seconds of downtime in the period")
    ongoing = row.get("outages_ongoing_at_period_start")
    if ongoing:
        text += f"; {_num_text(ongoing)} outages were already ongoing at the period start"
    if row.get("duration_is_lower_bound"):
        text += "; an outage is still open, so the downtime is a lower bound"
    return text + f".{_refs_text(row)}"


def _generic_line(columns: Sequence[str], row: Mapping[str, Any]) -> str:
    parts = []
    for col in columns:
        value = row.get(col)
        if isinstance(value, (dict, list)):
            continue
        parts.append(f"{col}: " + ("not available" if value is None else _num_text(value)
                                    if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)))
    return "- " + "; ".join(parts) + f".{_refs_text(row)}"


def deterministic_fallback(result: Any) -> str:
    """English summary built ONLY from the original result values. Never recomputes, never rounds, never converts."""
    r = as_result_dict(result)
    tool, status = r.get("tool"), r.get("status")
    params = r.get("parameters") or {}
    scope = f"building {params.get('building')}" if params.get("building") else "the requested scope"
    period = f", {params.get('start')} to {params.get('end')}" if params.get("start") and params.get("end") else ""
    lines = [f"Verified summary of {tool} for {scope}{period} (result {r.get('result_id')}, status {status})."]
    rows = r.get("rows") or []
    if status in ("UNSUPPORTED", "CANNOT_COMPUTE"):
        lines.append("This result could not be computed, so there are no counts to report; this is not a count of zero.")
    elif not rows:
        lines.append("No rows were returned and this is a confirmed empty result." if status == "COMPLETE" and
                     not r.get("truncated") else
                     "No rows were returned, but the result is not complete, so this is not a confirmed zero.")
    else:
        line = {"doors.after_hours": _doors_line, "devices.offline_frequency": _devices_line}.get(tool)
        for row in rows:
            lines.append(line(row) if line else _generic_line(r.get("columns") or [], row))
        if r.get("truncated"):
            lines.append(f"Only part of the result is shown: {_num_text(len(rows))} of {_num_text(r.get('total_rows'))} rows.")
    for lim in r.get("limitations") or []:
        lines.append(f"Limitation {lim.get('code')}: {lim.get('message')}")
    return "\n".join(lines)


def unverifiable_text(result_id: Any, codes: Sequence[str]) -> str:
    """Used when the result or its evidence cannot be trusted: no values are repeated."""
    shown = ", ".join(sorted(set(codes))) or "unknown"
    return (f"No summary is produced for result {result_id}: its evidence or integrity could not be verified "
            f"({shown}). The stored result is unchanged.")
