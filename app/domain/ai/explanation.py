"""Evidence-bounded alert explanation policy with deterministic fallback."""
import json
import re

from app.domain.ai.model_provider import ModelProvider
from app.domain.ai.recommendations import eligible
from app.domain.evidence.grounding import (
    build_package, check_text, normalize_summary, parse_citations,
)

SYSTEM_PROMPT = """You are an assistant for a building operations team. You receive ONE alert as JSON evidence.
Rules:
1. Use only facts, numbers and times that appear in the evidence. Do not calculate new numbers, do not estimate, and do not mention devices or causes that are not in the evidence.
2. The summary MUST cite events using ONLY refs from the evidence (e.g. [E1], [E2]). Every statement about a reading needs a citation.
Square brackets are reserved for event refs such as [E2]. Refer to causes and actions in plain words, never with brackets or codes like [C1].
3. Choose cause_ids and action_ids only from allowed_causes and allowed_actions. Do not write new causes or actions.
4. Times are UTC in HH:MM form. Write times exactly as they appear in the evidence.
5. If the evidence is not enough to name a cause, say so in the summary and return an empty cause_ids list.
Return JSON only: {"summary": "2-3 sentences with citations", "cause_ids": [], "action_ids": [], "evidence_refs": [], "confidence": "low|medium|high"}"""


def parse_json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        match = re.search(r"\{.*\}", raw, re.S)
        if match:
            return json.loads(match.group(0))
        raise


def _finish(summary, causes, actions, pkg, source, confidence, grounding, model=None):
    return {"summary": summary, "probable_causes": causes, "recommended_actions": actions,
            "evidence_refs": [event["ref"] for event in pkg["events"]], "confidence": confidence,
            "source": source, "model": model, "grounding": grounding}


def _fallback(alert, pkg, causes, actions, note=None):
    refs = [event["ref"] for event in pkg["events"]]
    cite = ", ".join(f"[{ref}]" for ref in (refs[:1] + refs[-1:] if len(refs) > 1 else refs))
    summary = f"{alert['summary']} Evidence: {cite}."
    violations = check_text(summary, pkg)
    grounding = {"passed": not violations, "violations": violations, **(note or {})}
    confidence = "medium" if any(
        cause["supported_by"] != ["generic possibility for this rule"] for cause in causes
    ) else "low"
    return _finish(summary, causes, actions, pkg, "FALLBACK", confidence, grounding)


def explain(alert, asset, *, use_llm=True, provider: ModelProvider | None = None):
    pkg = build_package(alert, asset)
    causes, actions = eligible(alert["rule_id"], alert["evidence"]["facts"])
    if not use_llm:
        return _fallback(alert, pkg, causes, actions, {"llm_attempted": False})
    if provider is None:
        return _fallback(alert, pkg, causes, actions,
                         {"llm_attempted": True, "llm_error": "No model provider is configured."})

    user = json.dumps({
        "evidence": pkg,
        "allowed_causes": [{"id": cause["id"], "text": cause["text"]} for cause in causes],
        "allowed_actions": [{"id": action["id"], "text": action["text"]} for action in actions],
        "reminder": "Cite events only as [E<number>] with refs that exist in evidence.events, one ref per bracket. "
                    "Never put causes, actions or ids such as FAN_FAILURE or C1 in brackets; name them in plain words. "
                    "Use only numbers and times that appear in evidence."
    })
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
    rejected = []
    for attempt in (1, 2):
        try:
            reply = provider.complete(messages=messages, response_format="json", options={"temperature": 0})
            data = parse_json(reply.content)
        except Exception as exc:
            return _fallback(alert, pkg, causes, actions,
                             {"llm_attempted": True, "llm_error": f"{type(exc).__name__}: {exc}"[:200],
                              "rejected_answers": rejected})

        summary = str(data.get("summary", "")).strip()
        summary, normalized = normalize_summary(summary)
        violations = check_text(summary, pkg) if summary else ["empty summary"]
        refs = {event["ref"] for event in pkg["events"]}
        for evidence_ref in data.get("evidence_refs", []):
            got, malformed = parse_citations(f"[{evidence_ref}]")
            if malformed or any(ref not in refs for ref in got):
                violations.append(f"unknown evidence_ref {evidence_ref}")

        if not violations:
            cause_ids = [cause for cause in data.get("cause_ids", []) if cause in {item["id"] for item in causes}]
            action_ids = [action for action in data.get("action_ids", []) if action in {item["id"] for item in actions}]
            chosen_causes = [cause for cause in causes if cause["id"] in cause_ids]
            chosen_actions = [action for action in actions if action["id"] in action_ids] or actions
            dropped = [item for item in data.get("cause_ids", []) + data.get("action_ids", [])
                       if item not in cause_ids + action_ids]
            confidence = data.get("confidence") if data.get("confidence") in ("low", "medium", "high") else "low"
            return _finish(summary, chosen_causes, chosen_actions, pkg, "LLM", confidence,
                           {"passed": True, "violations": [], "attempts": attempt,
                            "rejected_answers": rejected, "normalized": normalized,
                            "ignored_ids_not_in_playbook": dropped}, getattr(provider, "model", None))

        rejected.append({"attempt": attempt, "violations": violations})
        messages += [
            {"role": "assistant", "content": json.dumps(data)},
            {"role": "user", "content": "Your answer was rejected: " + "; ".join(violations)
             + ". Rewrite it using only numbers, times and citations present in the evidence."},
        ]
    return _fallback(alert, pkg, causes, actions,
                     {"llm_attempted": True, "llm_error": "answer failed grounding validation twice",
                      "rejected_answers": rejected})
