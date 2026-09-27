"""The catalog wire's ``model_facts``: description, release date, recency, pricing, size.

:func:`model_facts` projects the descriptive facts of one
:class:`~clio_agent.providers.capabilities.combine.EffectiveCapabilities` onto
the catalog row, next to ``capability_tags``. Every fact is
``{"value": ..., "evidence": [...]}`` with the same evidence rows a capability
tag carries (``source`` / ``detail`` / ``observed_at``), or ``None`` when no
source states it -- a picker shows nothing for it and never guesses.

Shapes (all slider-friendly: the number a slider filters on is a plain JSON
number, and a price that is not a number is a typed ``kind`` with no number):

* ``description.value`` -- ``{"text": raw, "plain": text with markdown links
  reduced to their label, "links": [{"text", "url"}]}``.
* ``released_at.value`` -- ``{"date": "2025-04-27" | "2026-02", "precision":
  "day" | "month" | "year"}``.
* ``recent`` -- derived HERE, at serve time, from ``released_at`` (never
  stored): ``value`` is whether the release is within
  :data:`RECENT_WINDOW_MONTHS` of ``as_of``; its evidence is the release
  date's.
* ``pricing.value`` -- ``{"unit": "usd_per_1m_tokens", "input": {"kind":
  "usd" | "variable" | "subscription", "per_1m": number | null}, "output":
  {...}}``. What THIS endpoint charges wins; a catalog list price (LiteLLM)
  is the fallback, and whichever did not win is listed under
  ``alternatives`` with its own evidence.
* ``parameters.value`` -- ``{"total": int | null, "active": int | null,
  "experts_total": int | null, "experts_active": int | null, "precision":
  "exact" | "rounded"}``; ``total`` is the slider value.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timezone
from typing import Any

from clio_agent.providers.capabilities.combine import Decision, EffectiveCapabilities
from clio_agent.providers.capabilities.model_facts import (
    ParameterCount,
    Price,
    ReleaseDate,
    TokenPricing,
)
from clio_agent.providers.capabilities.tags import decision_evidence

#: A release within this many calendar months of ``as_of`` is ``recent``.
RECENT_WINDOW_MONTHS = 6

#: An inline markdown link: ``[label](url)``.
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\(\s*([^)\s]+)\s*\)")


def plain_text(text: str) -> tuple[str, list[dict[str, str]]]:
    """``text`` with every markdown link reduced to its label, plus the links as data."""
    links = [{"text": label, "url": url} for label, url in _MARKDOWN_LINK.findall(text)]
    return _MARKDOWN_LINK.sub(lambda match: match.group(1), text), links


def months_before(day: date, months: int) -> date:
    """The same day ``months`` calendar months earlier (clamped to the month's end)."""
    year, month_index = divmod(day.year * 12 + (day.month - 1) - months, 12)
    month = month_index + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def is_recent(released: ReleaseDate, as_of: date) -> bool:
    """Whether ``released`` (its earliest possible day) is within the recent window of ``as_of``."""
    return released.earliest() > months_before(as_of, RECENT_WINDOW_MONTHS)


def _fact(value: Any, decision: Decision[Any]) -> dict[str, Any] | None:
    """One wire fact, or ``None`` when no source can be named for it."""
    evidence = decision_evidence(decision)
    return {"value": value, "evidence": evidence} if evidence else None


def _price(price: Price) -> dict[str, Any]:
    per_1m = float(price.per_1m) if price.per_1m is not None else None
    return {"kind": price.kind, "per_1m": per_1m}


def pricing_value(pricing: TokenPricing) -> dict[str, Any]:
    """The wire shape of one :class:`TokenPricing`."""
    return {
        "unit": "usd_per_1m_tokens",
        "input": _price(pricing.input),
        "output": _price(pricing.output),
    }


def _pricing(effective: EffectiveCapabilities) -> dict[str, Any] | None:
    stated = [
        (decision.value, decision)
        for decision in (effective.pricing, effective.catalog_pricing)
        if isinstance(decision.value, TokenPricing) and decision_evidence(decision)
    ]
    if not stated:
        return None
    (winner, winning), *others = stated
    fact = _fact(pricing_value(winner), winning)
    if fact is None:
        return None
    fact["alternatives"] = [
        {"value": pricing_value(value), "evidence": decision_evidence(decision)}
        for value, decision in others
        if value != winner
    ]
    return fact


def _parameters(count: ParameterCount) -> dict[str, Any]:
    return {
        "total": count.total,
        "active": count.active,
        "experts_total": count.experts_total,
        "experts_active": count.experts_active,
        "precision": count.precision,
    }


def model_facts(
    effective: EffectiveCapabilities, *, model_key: str, as_of: date | None = None
) -> dict[str, Any]:
    """The ``model_facts`` block of one catalog row.

    Args:
        effective: The combined view for ``(provider_id, api_base, model_id)``.
        model_key: The identity the facts describe (as ``capability_tags``).
        as_of: The day ``recent`` is judged against; today (UTC) when omitted.

    Returns:
        ``{"model_key", "description", "released_at", "recent", "pricing",
        "parameters"}``; each fact is ``None`` when no source states it.
    """
    as_of = as_of or datetime.now(timezone.utc).date()
    description = None
    if effective.description.known:
        text = str(effective.description.value)
        plain, links = plain_text(text)
        description = _fact({"text": text, "plain": plain, "links": links}, effective.description)
    released = effective.released_at
    released_fact = recent_fact = None
    if released.known and isinstance(released.value, ReleaseDate):
        release = released.value
        released_fact = _fact({"date": release.value, "precision": release.precision}, released)
        if released_fact is not None:
            recent_fact = {
                "value": is_recent(release, as_of),
                "window_months": RECENT_WINDOW_MONTHS,
                "as_of": as_of.isoformat(),
                "evidence": released_fact["evidence"],
            }
    parameters = effective.parameters
    return {
        "model_key": model_key,
        "description": description,
        "released_at": released_fact,
        "recent": recent_fact,
        "pricing": _pricing(effective),
        "parameters": (
            _fact(_parameters(parameters.value), parameters)
            if parameters.known and isinstance(parameters.value, ParameterCount)
            else None
        ),
    }


__all__ = [
    "RECENT_WINDOW_MONTHS",
    "is_recent",
    "model_facts",
    "months_before",
    "plain_text",
    "pricing_value",
]
