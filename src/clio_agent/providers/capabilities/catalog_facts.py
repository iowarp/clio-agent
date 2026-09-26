"""Descriptive model facts from the fetched community catalogs (models.dev, LiteLLM).

The community-catalog tier (brief 5.1 step 5) for the facts in
:mod:`clio_agent.providers.capabilities.model_facts`:

* models.dev (``models.json``, fetched through the shared
  :mod:`~clio_agent.providers.handshake.sources.models_dev` cache) --
  ``description``, ``release_date`` (month or day precision, kept as stated),
  and a ``weights`` link to the Hugging Face repo the weights live in (the Hub
  layer then reads the parameter count through it). models.dev's
  ``models.json`` carries no cost and no parameter count, so it states neither.
* LiteLLM's community cost map -- ``input_cost_per_token`` /
  ``output_cost_per_token``, as the catalog list price (per 1M tokens).

Both catalogs are read through the SAME id normalization every other
community-catalog lookup uses; nothing here reads a size or a date out of a
model id.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.capabilities.model_facts import (
    pricing_from_per_token,
    release_from_text,
)
from clio_agent.providers.capabilities.records import Fact, ModelCapabilities, unknown

#: A models.dev ``weights[].url`` that names a Hub repo.
_HF_URL = re.compile(r"^https?://huggingface\.co/([A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*)/?$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hf_repo_from_weights(weights: Any) -> tuple[str | None, str]:
    """The Hub repo a models.dev ``weights`` list links to, with the link as detail."""
    if not isinstance(weights, list):
        return None, ""
    for item in weights:
        url = item.get("url") if isinstance(item, dict) else None
        if not isinstance(url, str):
            continue
        match = _HF_URL.match(url.strip())
        if match is not None:
            return match.group(1), url.strip()
    return None, ""


def _models_dev_facts(model_id: str, *, allow_fetch: bool, observed_at: str) -> dict[str, Fact]:
    from clio_agent.providers.handshake.sources import models_dev  # noqa: PLC0415

    entry = models_dev.lookup_models_dev_entry(model_id, allow_fetch=allow_fetch)
    if not isinstance(entry, dict):
        return {}
    entry_id = str(entry.get("id") or model_id)
    facts: dict[str, Fact] = {}
    description = entry.get("description")
    if isinstance(description, str) and description.strip():
        facts["description"] = Fact(
            description, "models.dev", observed_at, f"models.dev {entry_id} description"
        )
    released = release_from_text(entry.get("release_date"))
    if released is not None:
        facts["released_at"] = Fact(
            released,
            "models.dev",
            observed_at,
            f"models.dev {entry_id} release_date={entry.get('release_date')!r}",
        )
    repo, url = hf_repo_from_weights(entry.get("weights"))
    if repo is not None:
        facts["hf_repo"] = Fact(
            repo, "models.dev", observed_at, f"models.dev {entry_id} weights {url}"
        )
    return facts


def _litellm_facts(model_id: str, *, allow_fetch: bool, observed_at: str) -> dict[str, Fact]:
    from clio_agent.providers.handshake.sources import litellm_catalog  # noqa: PLC0415

    matched = litellm_catalog.lookup_litellm_info(model_id, allow_fetch=allow_fetch)
    if matched is None:
        return {}
    key, info = matched
    prompt, completion = info.get("input_cost_per_token"), info.get("output_cost_per_token")
    pricing = pricing_from_per_token(prompt, completion)
    if pricing is None:
        return {}
    detail = f"litellm {key} input_cost_per_token={prompt!r} output_cost_per_token={completion!r}"
    return {"catalog_pricing": Fact(pricing, "litellm", observed_at, detail)}


def descriptive_catalog_facts(
    model_id: str, *, allow_fetch: bool = True
) -> ModelCapabilities | None:
    """Description, release date, Hub link and list price from the community catalogs.

    Args:
        model_id: The wire/catalog id (normalized the same way every catalog
            lookup normalizes it).
        allow_fetch: ``False`` reads the disk caches only (a passive read path
            such as the CLI catalog handshake must never touch the network).

    Returns:
        A record carrying only the facts a catalog states (every other field
        unknown), or ``None`` when neither catalog states any of them.
    """
    if not (model_id or "").strip():
        return None
    observed_at = _now_iso()
    facts = {
        **_models_dev_facts(model_id, allow_fetch=allow_fetch, observed_at=observed_at),
        **_litellm_facts(model_id, allow_fetch=allow_fetch, observed_at=observed_at),
    }
    if not facts:
        return None
    return ModelCapabilities(
        model_key=model_id,
        description=facts.get("description", unknown()),
        released_at=facts.get("released_at", unknown()),
        catalog_pricing=facts.get("catalog_pricing", unknown()),
        hf_repo=facts.get("hf_repo", unknown()),
    )


__all__ = ["descriptive_catalog_facts", "hf_repo_from_weights"]
