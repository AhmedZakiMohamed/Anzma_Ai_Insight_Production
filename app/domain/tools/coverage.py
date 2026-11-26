"""Ingestion-coverage hook (ADR-0001 open question g).

Nothing in the platform records whether a connector retrieved *everything* for a period, so a result can only be
COMPLETE if trusted server-side code supplies a ``SourceCoverage`` through a ``CoverageProvider``. The provider is
wired when a tool is built; it is never a tool argument, so neither the model nor an API caller can claim completeness.
Without a provider every result is PARTIAL / COMPLETION_NOT_CONFIRMED.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Mapping, Sequence

from app.domain.tools.envelope import Limitation, LimitationCode


@dataclass(frozen=True)
class SourceCoverage:
    source_code: str
    supports_history_events: bool            # source manifest declares history.events
    retrieval_confirmed_complete: bool       # the connector confirmed it fetched every page for the range below
    covered_from: datetime | None            # aware UTC bounds of what was confirmed retrieved
    covered_to: datetime | None
    data_as_of: datetime | None              # last observation actually retrieved


# (building_code, start_utc, end_utc_exclusive, source_codes) -> coverage per source_code; None = no information
CoverageProvider = Callable[[str, datetime, datetime, Sequence[str]], "Mapping[str, SourceCoverage] | None"]


@dataclass(frozen=True)
class CoverageAssessment:
    confirmed_complete: bool
    all_unsupported: bool
    data_as_of: datetime | None
    limitations: tuple[Limitation, ...]
    # Last observation each source reported retrieving, whether or not coverage of the whole period is confirmed.
    # ``data_as_of`` above stays None unless everything is confirmed; tools that only need an upper time bound for an
    # open interval (devices.offline_frequency, D-09) read this instead. Missing source = unknown, never "now".
    source_data_as_of: Mapping[str, datetime | None] = field(default_factory=dict)


def assess_coverage(provider: CoverageProvider | None, building_code: str, source_codes: Sequence[str],
                    start_utc: datetime, end_utc: datetime, *, source_label: str = "door") -> CoverageAssessment:
    reports = provider(building_code, start_utc, end_utc, tuple(source_codes)) if provider and source_codes else None
    if not reports:
        reason = ("no coverage provider is configured" if provider is None else
                  f"no {source_label} sources are mapped for this building" if not source_codes else
                  "the coverage provider returned no information")
        return CoverageAssessment(False, False, None, (Limitation(
            LimitationCode.COMPLETION_NOT_CONFIRMED,
            "Ingestion completeness for this period is not confirmed, so the result may be missing events.",
            {"reason": reason, "sources": list(source_codes)}),))

    limitations, unsupported, unconfirmed, as_of = [], [], [], []
    for code in source_codes:
        rep = reports.get(code)
        if rep is None:
            unconfirmed.append(code)
        elif not rep.supports_history_events:
            unsupported.append(code)
        elif not (rep.retrieval_confirmed_complete and rep.covered_from and rep.covered_to
                  and rep.covered_from <= start_utc and rep.covered_to >= end_utc):
            unconfirmed.append(code)
        else:
            as_of.append(rep.data_as_of)
    if unsupported:
        limitations.append(Limitation(LimitationCode.CAPABILITY_UNSUPPORTED,
                                      "These sources do not declare the history.events capability.",
                                      {"sources": sorted(unsupported)}))
    if unconfirmed:
        limitations.append(Limitation(LimitationCode.COMPLETION_NOT_CONFIRMED,
                                      "Ingestion completeness for the whole period is not confirmed for these sources.",
                                      {"sources": sorted(unconfirmed)}))
    ok = not unsupported and not unconfirmed
    known = [t for t in as_of if t is not None]
    return CoverageAssessment(ok, len(unsupported) == len(source_codes), min(known) if ok and known else None,
                              tuple(limitations),
                              {c: reports[c].data_as_of for c in source_codes if reports.get(c) is not None})
