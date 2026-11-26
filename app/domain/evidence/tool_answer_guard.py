"""tool_answer_guard.py (Task 7): the single validation seam for answers about a Tool Envelope result.

Implemented and tested: ``guard_tool_answer`` and ``run_guarded_tool`` (below).
NOT implemented (reserved for Amr's integration): calling them from ``chat_lite._agent_answer``. Chat does not consume
the Tool Registry yet (``DEFAULT_REGISTRY`` is empty), so *no production request path reaches this module today*. The
only active production path changed by Task 7 is the alert evidence check (``pipeline.explain_alert`` ->
``evidence.alert_evidence`` -> building scope). See docs/EVIDENCE_VALIDATION.md, "Execution paths".

Integration contract for the chat layer
---------------------------------------
    sealed = seal_result(registry.call(name, arguments))     # BEFORE anything model-facing sees the result
    # ... show sealed.result to the model, get its text ...
    outcome = guard_tool_answer(answer=text, result=sealed, cur=readonly_cursor,
                                authorized_building_codes=trusted_codes_or_None)
    response = outcome.to_response()                          # existing standard_response() contract

or let ``run_guarded_tool`` do all of it with an ``answer_fn(result_dict) -> str`` callback.

What the guard decides
----------------------
* Fingerprint: the presented result (and, when given, an ``expected_fingerprint`` taken at tool-return time) must
  hash to the authoritative result's fingerprint.
* Evidence + building scope: ``validate_envelope_evidence`` (database, read-only). By default
  (``require_evidence=True``) a missing cursor is itself a failure: the guard fails closed rather than silently
  skipping the database check.
* Numbers: ``validate_numeric_claims`` against the authoritative metric fields.
* Output selection: model answer accepted only if every check passed. Otherwise the answer is replaced by a
  deterministic summary built from the ORIGINAL values; if the evidence or the result itself cannot be trusted, no
  values are repeated at all. The model is never asked to repair anything and the original result is never edited.

Response contract: ``validation.standard_response`` (status/scope/checks_run/errors/attempts). Extra keys
(``result_id``, ``result_fingerprint``, ``integrity``) are additive, like ``recommendations`` on alert responses.
New ``checks_run`` names introduced here are listed in docs/EVIDENCE_VALIDATION.md (the contract document must be
extended by its owner; it is not edited by Task 7).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Collection, Mapping

from app.domain.evidence import numeric_integrity as ni
from app.domain.evidence import validation as val
from app.domain.evidence.envelope_evidence import (EnvelopeEvidenceResult, validate_envelope_evidence)

EVIDENCE_RESOLUTION_UNAVAILABLE = "EVIDENCE_RESOLUTION_UNAVAILABLE"
FALLBACK_SELF_CHECK_FAILED = "FALLBACK_SELF_CHECK_FAILED"
PRESENTED_RESULT_MISMATCH = "PRESENTED_RESULT_MISMATCH"


@dataclass(frozen=True)
class GuardOutcome:
    answer: str                                  # final text: the model's answer if it passed, else deterministic
    model_answer_accepted: bool
    validation: dict[str, Any]                   # the existing contract object
    ai_source: str                               # llm | fallback | rules (contract values)
    data_sufficiency: str                        # complete | partial | no_data
    limitations: tuple[str, ...]
    result_id: str
    fingerprint: str
    evidence: tuple[dict[str, Any], ...]
    references: dict[str, list]
    issues: tuple[dict[str, Any], ...]           # structured form of validation["errors"]
    authoritative: Mapping[str, Any] = field(repr=False, default_factory=dict)

    def to_response(self, *, model_used: str | None = None, language: str = "en") -> dict[str, Any]:
        base = val.standard_response(
            answer=self.answer, evidence=list(self.evidence), references=self.references, model_used=model_used,
            tool_used=self.authoritative.get("tool"), ai_source=self.ai_source, data_sufficiency=self.data_sufficiency,
            validation=self.validation, limitations=list(self.limitations), window=None, language=language)
        return base | {"result_id": self.result_id, "result_fingerprint": self.fingerprint,
                       "integrity": {"model_answer_accepted": self.model_answer_accepted, "issues": list(self.issues)}}


def _dedupe(items):
    return list(dict.fromkeys(items))


def guard_tool_answer(*, answer: str | None, result: Any, cur=None, presented: Any = None,
                      expected_fingerprint: str | None = None,
                      authorized_building_codes: Collection[str] | None = None,
                      require_evidence: bool = True, check_temporal: bool = True,
                      attempts: int = 1) -> GuardOutcome:
    """Validate ``answer`` (text about ``result``) and return the text that may be shown.

    result: the AUTHORITATIVE Envelope / SealedResult / dict. It is read, never modified.
    presented: the result payload the response will actually carry, if it passed through untrusted code.
    authorized_building_codes: TRUSTED server-side context only; never pass anything a model or caller asserted.
    """
    auth = ni.as_result_dict(result)
    fingerprint = ni.result_fingerprint(auth)
    result_id = str(auth.get("result_id"))
    checks: list[str] = []
    issues: list[dict[str, Any]] = []
    limitations = [lim.get("message") for lim in auth.get("limitations") or [] if lim.get("message")]

    def add(code: str, message: str, source: str):
        issues.append({"code": code, "message": message, "source": source})

    # 1. fingerprint ---------------------------------------------------------------------------------
    authoritative_changed = expected_fingerprint is not None and expected_fingerprint != fingerprint
    presented_changed = False
    if expected_fingerprint is not None or presented is not None:
        checks.append("result_fingerprint")
    if authoritative_changed:
        add(ni.NumericIssueCode.RESULT_FINGERPRINT_MISMATCH,
            "the authoritative result no longer matches the fingerprint taken when the tool returned it", "fingerprint")
    if presented is not None:
        try:
            presented_changed = ni.result_fingerprint(presented) != fingerprint
        except (TypeError, ValueError):
            presented_changed = True
        if presented_changed:
            add(PRESENTED_RESULT_MISMATCH,
                "the result presented with the answer differs from the authoritative result", "fingerprint")

    # 2. evidence + building scope -------------------------------------------------------------------
    evidence_res: EnvelopeEvidenceResult | None = None
    evidence_untrusted = False
    if cur is None:
        if require_evidence:
            evidence_untrusted = True
            add(EVIDENCE_RESOLUTION_UNAVAILABLE,
                "no database cursor was supplied, so evidence references could not be resolved", "evidence")
        else:
            limitations.append("Evidence references were not resolved against the database in this call.")
    else:
        evidence_res = validate_envelope_evidence(cur, auth, authorized_building_codes=authorized_building_codes)
        checks += list(evidence_res.checks_run)
        for issue in evidence_res.blocking_issues:
            add(issue.code, issue.message, "evidence")
        evidence_untrusted = not evidence_res.ok
        for warning in evidence_res.warnings:
            limitations.append(f"{warning.code}: {warning.message}")

    # 3. numbers in the model answer -----------------------------------------------------------------
    has_answer = bool(answer and answer.strip())
    answer_numbers_bad = False
    if has_answer:
        numeric = ni.validate_numeric_claims(answer, auth, check_temporal=check_temporal)
        checks += list(numeric.checks_run)
        for issue in numeric.blocking_issues:
            add(issue.code, issue.message, "numeric")
        answer_numbers_bad = not numeric.ok
        for warning in numeric.warnings:
            limitations.append(f"{warning.code}: {warning.message}")

    # 4. choose the text -----------------------------------------------------------------------------
    untrusted = authoritative_changed or evidence_untrusted
    answer_bad = answer_numbers_bad or presented_changed
    model_accepted = has_answer and not untrusted and not answer_bad
    if model_accepted:
        text, ai_source = answer.strip(), "llm"
    elif untrusted:
        text = ni.unverifiable_text(result_id, [i["code"] for i in issues])
        ai_source = "fallback" if has_answer else "rules"
        limitations.append("The result's evidence or integrity could not be verified, so no summary was produced "
                           "from its values.")
    else:
        text = ni.deterministic_fallback(auth)
        selfcheck = ni.validate_numeric_claims(text, auth, check_temporal=check_temporal)
        if not has_answer:
            checks += list(selfcheck.checks_run)
        if not selfcheck.ok:                                   # defensive: a fallback must never contradict the result
            for issue in selfcheck.blocking_issues:
                add(FALLBACK_SELF_CHECK_FAILED, issue.to_error(), "fallback")
            text = ni.unverifiable_text(result_id, [FALLBACK_SELF_CHECK_FAILED])
            untrusted = True
        ai_source = "fallback" if has_answer else "rules"
        if has_answer:
            limitations.append("The model's answer failed the numeric integrity check, so this summary was built "
                               "deterministically from the original result values.")

    # 5. contract objects ----------------------------------------------------------------------------
    checks = _dedupe(checks)
    errors = [f"{i['code']}: {i['message']}" for i in issues]
    failed = bool(errors)
    validation = {"status": val.FAILED if failed else (val.GROUNDING_SUPPORTED if checks else val.NOT_VALIDATED),
                  "scope": val.SCOPE, "checks_run": checks, "errors": errors, "attempts": attempts}

    status = auth.get("status")
    if status in ("UNSUPPORTED", "CANNOT_COMPUTE"):
        sufficiency = val.NO_DATA
    elif untrusted:
        sufficiency = val.PARTIAL if evidence_res is not None and evidence_res.verified_refs else val.NO_DATA
    else:
        sufficiency = val.COMPLETE if auth.get("complete") else val.PARTIAL

    if evidence_res is not None and evidence_res.resolved:
        evidence_items = tuple(evidence_res.evidence_items())
    else:
        evidence_items = tuple({"kind": r.get("kind"), "id": r.get("id"), "verified": False}
                               for row in auth.get("rows") or [] for r in row.get("evidence_refs") or [])
    references = {"assets": evidence_res.asset_ids if evidence_res is not None else [], "incidents": []}
    return GuardOutcome(text, model_accepted, validation, ai_source, sufficiency, tuple(_dedupe(limitations)),
                        result_id, fingerprint, evidence_items, references, tuple(issues), auth)


def run_guarded_tool(registry, tool_name: str, arguments: Mapping[str, Any] | None, *, cur,
                     answer_fn: Callable[[dict[str, Any]], str | None] | None = None,
                     authorized_building_codes: Collection[str] | None = None,
                     require_evidence: bool = True) -> GuardOutcome:
    """Callable integration boundary: registry call -> seal -> (optional model answer) -> guard.

    ``registry`` is a ``ToolRegistry``; ``tool_name`` / ``arguments`` come from the model and are validated by
    ``ToolRegistry.call`` as before (bad arguments return a CANNOT_COMPUTE envelope; an unknown tool raises
    ``ToolRegistryError``, a caller bug). ``answer_fn`` receives a COPY of the sealed result and returns the text, or
    None/raises when no model answer exists, in which case the deterministic summary is produced ("rules").
    ``cur`` must be a dict-row cursor in a READ ONLY transaction. Nothing calls this from Chat yet.
    """
    sealed = ni.seal_result(registry.call(tool_name, arguments))
    text = None
    if answer_fn is not None:
        try:
            text = answer_fn(sealed.result)
        except Exception:                                        # model unavailable: same as no answer
            text = None
    return guard_tool_answer(answer=text, result=sealed, cur=cur, expected_fingerprint=sealed.fingerprint,
                             authorized_building_codes=authorized_building_codes, require_evidence=require_evidence)
