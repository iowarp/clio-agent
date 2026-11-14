"""Each provider's published terms per access tier, which the request queue obeys.

A *tier* is how CLIO reaches a provider:

* ``keyless``: no account; the allowance is shared by everyone on the network.
* ``registered``: no key, but identified (Crossref's polite pool via the contact email).
* ``key``: the person's own key on the provider's free plan.
* ``paid``: the person's key on a paid plan (they say so in Settings); no CLIO cap.

Figures are the providers' own published terms (November 2026). ``None`` means
the provider publishes no number; CLIO then learns the limit from its
too-many-requests answers and rate-limit headers (see :mod:`.scheduler`).
"""

from __future__ import annotations

from dataclasses import dataclass

from clio_agent.search.settings import SearchSettings

KEYLESS = "keyless"
REGISTERED = "registered"
KEY = "key"
PAID = "paid"

TIER_WORDS = {
    KEYLESS: "No key: shared by everyone on your network",
    REGISTERED: "Identified with your contact email",
    KEY: "Your key (free plan)",
    PAID: "Your key (paid plan)",
}


@dataclass(frozen=True)
class Terms:
    """How fast and how much CLIO may ask one provider on one tier."""

    tier: str
    #: Most requests started per second (``None``: unpublished).
    per_second: float | None = None
    #: Most requests in flight at once.
    concurrency: int = 3
    #: Free allowance: most requests per hour / per day, or dollars per day.
    per_hour: int | None = None
    per_day: int | None = None
    usd_per_day: float | None = None

    @property
    def interval_s(self) -> float:
        """Least time between two request starts."""

        return 1.0 / self.per_second if self.per_second else 0.0

    def allowance(self) -> tuple[float, str, str] | None:
        """``(limit, unit, period)`` of the free allowance, or ``None`` when uncapped."""

        if self.usd_per_day is not None:
            return (self.usd_per_day, "usd", "day")
        if self.per_day is not None:
            return (float(self.per_day), "requests", "day")
        if self.per_hour is not None:
            return (float(self.per_hour), "requests", "hour")
        return None


_PAID = Terms(PAID, per_second=None, concurrency=8)

#: Declared terms: provider → tier → terms. A tier a provider does not offer is absent.
TERMS: dict[str, dict[str, Terms]] = {
    "keenable": {
        # One per deep-research researcher; push-back lowers it when Keenable objects.
        KEYLESS: Terms(KEYLESS, concurrency=8, per_hour=1000),
        KEY: Terms(KEY, concurrency=6),
        PAID: _PAID,
    },
    "exa": {KEYLESS: Terms(KEYLESS, concurrency=3), KEY: Terms(KEY, concurrency=5), PAID: _PAID},
    "firecrawl": {
        KEYLESS: Terms(KEYLESS, concurrency=2),
        KEY: Terms(KEY, concurrency=2),
        PAID: _PAID,
    },
    "you": {KEYLESS: Terms(KEYLESS, concurrency=2, per_day=100), KEY: Terms(KEY), PAID: _PAID},
    "parallel": {KEYLESS: Terms(KEYLESS, concurrency=3), KEY: Terms(KEY), PAID: _PAID},
    "service": {KEYLESS: Terms(KEYLESS, concurrency=8), KEY: Terms(KEY, concurrency=8)},
    "openalex": {
        KEYLESS: Terms(KEYLESS, per_second=10, concurrency=2, usd_per_day=0.10),
        KEY: Terms(KEY, per_second=10, concurrency=4, usd_per_day=1.0),
        PAID: Terms(PAID, per_second=50, concurrency=8),
    },
    "crossref": {
        KEYLESS: Terms(KEYLESS, per_second=5, concurrency=1),
        REGISTERED: Terms(REGISTERED, per_second=10, concurrency=3),
    },
    "arxiv": {KEYLESS: Terms(KEYLESS, per_second=1 / 3, concurrency=1)},
    "europepmc": {KEYLESS: Terms(KEYLESS, per_second=10, concurrency=4)},
    "osti": {KEYLESS: Terms(KEYLESS, per_second=5, concurrency=2)},
    "opencitations": {KEYLESS: Terms(KEYLESS, per_second=5, concurrency=2)},
    "datacite": {KEYLESS: Terms(KEYLESS, per_second=5, concurrency=2)},
    "semantic_scholar": {KEY: Terms(KEY, per_second=1, concurrency=1), PAID: _PAID},
}

#: What one OpenAlex request costs (dollars), by the kind of request.
OPENALEX_COST = {"work": 0.0, "list": 0.0001, "search": 0.001}

_DEFAULT = Terms(KEYLESS, concurrency=2)


def tier_of(source: str, settings: SearchSettings, *, has_key: bool) -> str:
    """The tier CLIO reaches ``source`` on, from the key, the paid mark and the email."""

    offered = TERMS.get(source, {})
    if has_key and source in settings.paid and PAID in offered:
        return PAID
    if has_key and KEY in offered:
        return KEY
    if source == "crossref" and settings.contact_email:
        return REGISTERED
    return KEYLESS if KEYLESS in offered or not offered else next(iter(offered))


def terms_of(source: str, settings: SearchSettings, *, has_key: bool) -> Terms:
    """The terms CLIO obeys for ``source`` right now."""

    return terms_for(source, tier_of(source, settings, has_key=has_key))


def terms_for(source: str, tier: str) -> Terms:
    """The declared terms of ``source`` on ``tier`` (a cautious default when unknown)."""

    return TERMS.get(source, {}).get(tier) or _DEFAULT


__all__ = [
    "KEY",
    "KEYLESS",
    "OPENALEX_COST",
    "PAID",
    "REGISTERED",
    "TERMS",
    "TIER_WORDS",
    "Terms",
    "terms_for",
    "terms_of",
    "tier_of",
]
