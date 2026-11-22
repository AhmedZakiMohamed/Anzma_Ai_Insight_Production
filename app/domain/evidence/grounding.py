"""Pure evidence packaging and grounding checks for alert explanations."""
import re
from datetime import datetime, timezone

import app.domain.alerts.alert_config as cfg

CITE = re.compile(r"\[([^\[\]]*)\]")
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
TIME = re.compile(r"\b\d{1,2}:\d{2}\b")
NUM_STRICT = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
NUM_ANY = re.compile(r"\d+(?:\.\d+)?")
PAREN_REF = re.compile(r"\((E\d+)\)")


def parse_citations(text):
    """Return (refs, malformed). Accept [E3], [E1, E4], and [E1-E5]."""
    refs, malformed = [], []
    for body in CITE.findall(text):
        for part in (p.strip() for p in body.split(",")):
            single = re.fullmatch(r"E(\d+)", part)
            span = re.fullmatch(r"E(\d+)\s*[-–]\s*E(\d+)", part)
            if single:
                refs.append(f"E{single.group(1)}")
            elif span and int(span.group(1)) <= int(span.group(2)) <= 999:
                refs.extend(f"E{i}" for i in range(int(span.group(1)), int(span.group(2)) + 1))
            else:
                malformed.append(f"[{body}]")
    return refs, malformed


def parse_ts(value):
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def build_package(alert, asset):
    """Build the evidence-only package exposed to a model or deterministic renderer."""
    ev = alert["evidence"]
    rule = cfg.RULES[alert["rule_id"]]
    events = []
    for event in ev["events"]:
        events.append({"ref": event["ref"], "time_utc": parse_ts(event["ts"]).strftime("%H:%M"),
                       "event_type": event["event_type"], "value": event["value"], "unit": event["unit"],
                       "details": event["details"]})
    package = {
        "alert": {"rule": rule["title"], "severity": alert["severity"],
                  "method": alert["detection_method"], "asset": asset["name"],
                  "system": asset["system_type"], "zone": asset["zone"],
                  "first_utc": parse_ts(alert["first_ts"]).strftime("%Y-%m-%d %H:%M"),
                  "last_utc": parse_ts(alert["last_ts"]).strftime("%Y-%m-%d %H:%M")},
        "thresholds": ev["thresholds"], "facts": ev["facts"],
        "event_count": ev["event_count"], "events": events,
    }
    data_path = alert.get("_g3_evidence")
    if data_path:
        package["data_path"] = _data_path_json(data_path)
    return package


def _data_path_json(value):
    """Keep private G3 timestamps JSON-safe and canonical UTC for model grounding."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, dict):
        return {str(key): _data_path_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_data_path_json(item) for item in value]
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


def allowed_values(pkg):
    nums, times, dates = [], set(), set()

    def walk(value, key=None):
        if value is None or isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            nums.append(float(value))
        elif isinstance(value, str):
            if key == "time_utc":
                times.add(value)
            elif key in ("first_utc", "last_utc"):
                date, time = value.split(" ")
                dates.add(date)
                times.add(time)
            elif key.endswith("_at_utc"):
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    dates.add(parsed.strftime("%Y-%m-%d"))
                    times.add(parsed.strftime("%H:%M"))
                except ValueError:
                    nums.extend(float(n) for n in NUM_ANY.findall(value))
            elif key != "ref":
                nums.extend(float(n) for n in NUM_ANY.findall(value))
        elif isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, child_key)
        elif isinstance(value, list):
            for child in value:
                walk(child, key)

    walk(pkg)
    return nums, times, dates


def number_ok(number, allowed):
    for value in allowed:
        if abs(value - number) <= 0.051:
            return True
        if float(number).is_integer() and (round(value) == number or int(value) == number):
            return True
    return False


def check_text(text, pkg):
    """Return grounding violations against the numeric, date, time, and citation evidence."""
    nums, times, dates = allowed_values(pkg)
    refs = {event["ref"] for event in pkg["events"]}
    bad = []
    citations, malformed = parse_citations(text)
    if not citations:
        bad.append("no event citation")
    bad += [f"malformed citation {item}" for item in dict.fromkeys(malformed)]
    for citation in dict.fromkeys(citations):
        if citation not in refs:
            bad.append(f"unknown citation [{citation}]")
    remaining = CITE.sub(" ", text)
    for date in DATE.findall(remaining):
        if date not in dates:
            bad.append(f"date {date} not in evidence")
    remaining = DATE.sub(" ", remaining)
    for time in TIME.findall(remaining):
        if time.zfill(5) not in times:
            bad.append(f"time {time} not in evidence")
    remaining = TIME.sub(" ", remaining)
    for token in NUM_STRICT.findall(remaining):
        if not number_ok(float(token), nums):
            bad.append(f"number {token} not in evidence")
    return bad


def normalize_summary(text):
    """Normalize citation formatting without adding refs or changing evidence values."""
    notes = []

    def normalize_parentheses(match):
        notes.append(f"{match.group(0)} -> [{match.group(1)}]")
        return f"[{match.group(1)}]"

    text = PAREN_REF.sub(normalize_parentheses, text)

    def remove_invalid_bracket(match):
        body = match.group(1)
        parts = [part.strip() for part in body.split(",")]
        if all(re.fullmatch(r"E\d+(\s*[-–]\s*E\d+)?", part) for part in parts):
            return match.group(0)
        notes.append(f"removed [{body}]")
        return ""

    text = CITE.sub(remove_invalid_bracket, text)
    text = re.sub(r"\s+([.,;])", r"\1", text)
    text = re.sub(r" {2,}", " ", text).strip()
    return text, notes
