"""Descriptive model facts: description, release date, pricing and parameter count.

These are the facts a model picker filters with sliders and explains routers
with. Each is recorded as a :class:`~clio_agent.providers.capabilities.records.Fact`
like every other capability, so a value always names the source that stated it,
and a value no source states stays unknown. Nothing here reads a model's NAME:
a size is only ever a source's own parameter-count field (Hugging Face
``safetensors.total``, Ollama ``general.parameter_count``, llama.cpp
``meta.n_params``, ...), never a ``-7b`` suffix.

The value types are small frozen records:

* :class:`ReleaseDate` -- an ISO date at the precision the source states it
  (``2025-04-27`` from a timestamp, ``2026-02`` from a month-only
  ``release_date``).
* :class:`Price` / :class:`TokenPricing` -- one side's price per 1M tokens as a
  :class:`~decimal.Decimal`, or a typed ``variable`` (the price depends on the
  routed model: OpenRouter's ``-1``) or ``subscription`` (billed through a
  plan, e.g. Claude Code / Codex) -- neither is ever a number, and neither is 0.
* :class:`ParameterCount` -- the total parameter count (the slider value), plus
  the active count and expert counts of a mixture-of-experts model when a
  source states them.

The constructors below convert each source's own spelling into these records
and return ``None`` for anything that is not a well-formed statement, so a
caller writes ``unknown()`` rather than a guess.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

#: How precisely a source states a release date.
DatePrecision = Literal["day", "month", "year"]

#: One side of a price: a metered USD rate, a price that depends on the routed
#: model, or a subscription plan.
PriceKind = Literal["usd", "variable", "subscription"]

#: Whether a parameter count is the exact tensor total or a size a server
#: rounds for display (Ollama ``details.parameter_size='7.6B'``).
CountPrecision = Literal["exact", "rounded"]

_PER_MILLION = Decimal(1_000_000)

#: OpenRouter's price for "depends on the routed model".
_VARIABLE_PRICE = "-1"


@dataclass(frozen=True)
class ReleaseDate:
    """When a model was released, at the precision its source states.

    Attributes:
        value: ``YYYY-MM-DD``, ``YYYY-MM`` or ``YYYY``.
        precision: ``day``, ``month`` or ``year``.
    """

    value: str
    precision: DatePrecision

    def earliest(self) -> date:
        """The first day this release date can mean (``2026-02`` -> 2026-02-01)."""
        parts = [int(part) for part in self.value.split("-")]
        while len(parts) < 3:
            parts.append(1)
        return date(parts[0], parts[1], parts[2])


@dataclass(frozen=True)
class Price:
    """One side (input or output) of a model's price.

    Attributes:
        kind: ``usd`` (metered, ``per_1m`` set), ``variable`` or ``subscription``
            (``per_1m`` is ``None``: there is no number to show or filter on).
        per_1m: USD per 1M tokens, exact.
    """

    kind: PriceKind
    per_1m: Decimal | None = None


@dataclass(frozen=True)
class TokenPricing:
    """Input and output prices, per 1M tokens."""

    input: Price
    output: Price

    @property
    def free(self) -> bool:
        """Both sides metered at exactly 0 (a variable or subscription price never is)."""
        return all(side.kind == "usd" and side.per_1m == 0 for side in (self.input, self.output))


#: A subscription plan bills the model; there is no per-token rate.
SUBSCRIPTION = TokenPricing(Price("subscription"), Price("subscription"))


@dataclass(frozen=True)
class ParameterCount:
    """A model's size in parameters, as its sources state it.

    Attributes:
        total: Every parameter (the slider value); ``None`` when a source states
            only the expert layout.
        active: Parameters used per token (mixture-of-experts), when stated.
        experts_total: Routed experts per MoE layer, when stated.
        experts_active: Experts chosen per token, when stated.
        precision: ``exact`` tensor total, or ``rounded`` display size.
    """

    total: int | None
    active: int | None = None
    experts_total: int | None = None
    experts_active: int | None = None
    precision: CountPrecision = "exact"

    @property
    def known(self) -> bool:
        """Whether this record states anything at all."""
        return any(
            value is not None
            for value in (self.total, self.active, self.experts_total, self.experts_active)
        )


def positive_int(value: Any) -> int | None:
    """``value`` when it is a positive ``int`` (never a bool), else ``None``."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


# --------------------------------------------------------------------------- release dates

_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?")


def release_from_unix(value: Any) -> ReleaseDate | None:
    """A release date from a unix timestamp (OpenRouter ``created``, OpenAI ``created``)."""
    seconds = positive_int(value)
    if seconds is None:
        return None
    try:
        moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return ReleaseDate(moment.date().isoformat(), "day")


def release_from_text(value: Any) -> ReleaseDate | None:
    """A release date from an ISO date/datetime string, keeping the stated precision.

    ``2026-02-14`` and ``2025-04-27T03:43:05.000Z`` are day precision;
    ``2026-02`` is month precision; ``2026`` is year precision. Anything else
    (an empty string, free text) is not a date.
    """
    if not isinstance(value, str):
        return None
    match = _DATE_RE.match(value.strip())
    if match is None:
        return None
    year, month, day = match.groups()
    try:
        date(int(year), int(month or 1), int(day or 1))
    except ValueError:
        return None
    if day is not None:
        return ReleaseDate(f"{year}-{month}-{day}", "day")
    if month is not None:
        return ReleaseDate(f"{year}-{month}", "month")
    return ReleaseDate(year, "year")


def release_from_any(value: Any) -> ReleaseDate | None:
    """A release date from a timestamp OR an ISO string (the overlay accepts both)."""
    if isinstance(value, date):
        return ReleaseDate(value.isoformat()[:10], "day")
    return release_from_unix(value) if isinstance(value, int) else release_from_text(value)


# --------------------------------------------------------------------------- pricing


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _per_million(per_token: Decimal) -> Decimal:
    scaled = per_token * _PER_MILLION
    # Drop trailing zeros but never switch to exponent notation for an integer.
    normalized = scaled.normalize()
    return normalized.quantize(Decimal(1)) if normalized == normalized.to_integral() else normalized


def price_from_per_token(value: Any) -> Price | None:
    """One side from a per-token price (OpenRouter's decimal strings, LiteLLM's floats).

    OpenRouter's ``"-1"`` is :data:`variable <PriceKind>`; a negative number
    otherwise, or anything that is not a number, states no price.
    """
    if isinstance(value, str) and value.strip() == _VARIABLE_PRICE:
        return Price("variable")
    number = _decimal(value)
    if number is None or number < 0:
        return None
    return Price("usd", _per_million(number))


def pricing_from_per_token(prompt: Any, completion: Any) -> TokenPricing | None:
    """Both sides from per-token prices, or ``None`` unless BOTH sides are stated."""
    input_price = price_from_per_token(prompt)
    output_price = price_from_per_token(completion)
    if input_price is None or output_price is None:
        return None
    return TokenPricing(input_price, output_price)


# --------------------------------------------------------------------------- parameter counts

#: A server's rounded display size (Ollama ``details.parameter_size``, LM Studio
#: ``params_string``): a number and a magnitude suffix, nothing else.
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMBT])\s*$", re.IGNORECASE)
_SIZE_SCALE = {"K": 10**3, "M": 10**6, "B": 10**9, "T": 10**12}


def parameters_from_size_field(value: Any) -> int | None:
    """A rounded count from a server's own parameter-SIZE field (``'7.6B'`` -> 7_600_000_000).

    Only for a field whose whole meaning is the model's size (never a model id
    or file name): a value that is not exactly ``<number><K|M|B|T>`` is no
    statement.
    """
    if not isinstance(value, str):
        return None
    match = _SIZE_RE.match(value)
    if match is None:
        return None
    number, suffix = match.groups()
    return int(Decimal(number) * _SIZE_SCALE[suffix.upper()])


#: Config keys (``config.json`` / Hub metadata ``config`` / GGUF metadata) that
#: state a mixture-of-experts layout, each as that architecture's own spelling.
EXPERT_TOTAL_KEYS: tuple[str, ...] = ("num_experts", "num_local_experts", "n_routed_experts")
EXPERT_ACTIVE_KEYS: tuple[str, ...] = ("num_experts_per_tok", "num_experts_per_token", "moe_topk")


def _first_positive(config: Mapping[str, Any], keys: tuple[str, ...]) -> tuple[int | None, str]:
    for key in keys:
        value = positive_int(config.get(key))
        if value is not None:
            return value, key
    return None, ""


def experts_from_config(config: Any) -> tuple[int | None, int | None, str]:
    """``(experts_total, experts_active, detail)`` from a model config, or ``(None, None, "")``.

    Reads the architecture's own expert-count keys (:data:`EXPERT_TOTAL_KEYS` /
    :data:`EXPERT_ACTIVE_KEYS`) at the top level, then in ``text_config`` (a
    multimodal model nests its language model's config there). A dense model
    states none of them.
    """
    if not isinstance(config, Mapping):
        return None, None, ""
    for scope, block in (("", config), ("text_config.", config.get("text_config"))):
        if not isinstance(block, Mapping):
            continue
        total, total_key = _first_positive(block, EXPERT_TOTAL_KEYS)
        active, active_key = _first_positive(block, EXPERT_ACTIVE_KEYS)
        if total is not None or active is not None:
            stated = [f"{scope}{k}={v}" for k, v in ((total_key, total), (active_key, active)) if k]
            return total, active, " ".join(stated)
    return None, None, ""


def parameters_from_overlay(value: Any) -> ParameterCount | None:
    """The overlay's curated ``parameters:`` -- an integer total, or ``{total, active}``."""
    if isinstance(value, Mapping):
        count = ParameterCount(
            total=positive_int(value.get("total")),
            active=positive_int(value.get("active")),
            experts_total=positive_int(value.get("experts")),
            experts_active=positive_int(value.get("expertsActive")),
        )
        return count if count.known else None
    total = positive_int(value)
    return ParameterCount(total=total) if total is not None else None


__all__ = [
    "EXPERT_ACTIVE_KEYS",
    "EXPERT_TOTAL_KEYS",
    "SUBSCRIPTION",
    "CountPrecision",
    "DatePrecision",
    "ParameterCount",
    "Price",
    "PriceKind",
    "ReleaseDate",
    "TokenPricing",
    "experts_from_config",
    "parameters_from_overlay",
    "parameters_from_size_field",
    "positive_int",
    "price_from_per_token",
    "pricing_from_per_token",
    "release_from_any",
    "release_from_text",
    "release_from_unix",
]
