"""Typed reasons for every search degradation, refusal and fallback (no silent fallback).

Each reason is a stable code carried in the tool result (``notes`` / ``error``) and
written to the runtime trace, so a person, a test or a later audit can see why a
search answered the way it did. The catalogue is the single list of codes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clio_agent.runtime import trace

#: Every search reason CLIO emits, with what it means.
SEARCH_REASONS: dict[str, str] = {
    "search_disabled": "Web search is turned off (search.enabled is false).",
    "search_no_source": "No web source is enabled; turn one on in Settings → Web search.",
    "search_waiting_for_person": "A question about search is waiting for the person's answer.",
    "search_not_approved": "The person declined to send queries to this source.",
    "search_stopped_by_person": "The person chose to stop searching when an allowance ran out.",
    "search_source_rate_limited": "The source answered too-many-requests; it cools down.",
    "search_source_allowance_used": "The source's free allowance is used up until it resets.",
    "search_source_key_rejected": "The source rejected the saved key.",
    "search_source_failed": "The source failed (network, server error or unreadable answer).",
    "search_source_retried": "The source failed once and was asked again before moving on.",
    "search_source_waited": "The request waited in CLIO's queue for the source's pace or limit.",
    "search_source_busy": "Other requests to the source were ahead in CLIO's queue too long.",
    "search_rotated": "The search moved to the next source in the person's order.",
    "search_all_sources_unavailable": "Every enabled source is cooling down or failed.",
    "search_filters_approximate": "A filter the source cannot apply was applied to its results.",
    "search_payload_not_saved": "The full source answer could not be saved to a file.",
    "fetch_failed": "The page could not be read.",
    "fetch_hosted_reader": "The page was read through a hosted reader after a local fetch failed.",
    "fetch_private_url": "A hosted reader was not used because the URL is private.",
    "search_source_temporarily_down": (
        "The source failed twice in a row (network or server error). That points to a "
        "temporary problem at the source, not to an empty result."
    ),
    "search_source_switched": "The source kept failing, so CLIO used the next source.",
    "search_source_refused_request": (
        "The source refused this one search as invalid; the next source took it."
    ),
    "literature_source_failed": "A literature source failed; the others answered.",
    "literature_source_budget_used": "A literature source's free daily budget is used up.",
}


@dataclass(frozen=True)
class SearchNote:
    """One typed reason attached to a search result."""

    code: str
    source: str = ""
    detail: str = ""
    until: str = ""
    #: What the agent can do about it, in words.
    advice: str = ""

    def as_dict(self) -> dict[str, Any]:
        """The note as it appears in a tool result."""

        row: dict[str, Any] = {"code": self.code, "message": SEARCH_REASONS[self.code]}
        if self.source:
            row["source"] = self.source
        if self.detail:
            row["detail"] = self.detail
        if self.until:
            row["until"] = self.until
        if self.advice:
            row["advice"] = self.advice
        return row


def note(
    code: str, source: str = "", detail: str = "", until: str = "", advice: str = ""
) -> SearchNote:
    """A typed note; unknown codes are a programming error, never a silent string."""

    if code not in SEARCH_REASONS:
        raise ValueError(f"unknown search reason {code!r}")
    trace.event("SEARCH", "%s source=%s %s", code, source or "-", detail[:200])
    return SearchNote(code=code, source=source, detail=detail, until=until, advice=advice)


__all__ = ["SEARCH_REASONS", "SearchNote", "note"]
