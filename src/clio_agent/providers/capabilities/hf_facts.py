"""Descriptive facts from a Hugging Face repo's public metadata: size and release date.

The Hub's ``GET /api/models/<repo>`` (already fetched and cached by
:mod:`clio_agent.providers.capabilities.hf_repo`, whose resolution pins the
commit SHA) states:

* ``safetensors.total`` (and the per-dtype ``safetensors.parameters``) -- the
  exact parameter count of the weights at that commit;
* ``createdAt`` -- when the repo was created, i.e. when the weights were
  published;
* ``config`` -- a subset of ``config.json``; for a mixture-of-experts model it
  carries the architecture's own expert counts (``num_experts`` /
  ``num_experts_per_tok``, ...). The full ``config.json``, when the Hub layer
  already read it, is preferred.

:func:`metadata_facts` is the pure reader; :class:`HfMetadataSource` is the
lighter Hub layer used when a NON-Hub endpoint (OpenRouter, a catalog link)
names the repo its weights are: it reads the metadata call only -- no
chat-template or config file reads -- because the endpoint itself already
states everything else about the model. A repo nobody linked is never looked
up: the parameter count of an unlinked model stays unknown.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from clio_agent.providers.capabilities.model_facts import (
    ParameterCount,
    experts_from_config,
    positive_int,
    release_from_text,
)
from clio_agent.providers.capabilities.records import Fact, ModelCapabilities, unknown

if TYPE_CHECKING:
    from clio_agent.providers.capabilities.hf_repo import RepoResolution


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safetensors_total(meta: Mapping[str, Any]) -> tuple[int | None, str]:
    """The exact parameter count the metadata's ``safetensors`` block states."""
    block = meta.get("safetensors")
    if not isinstance(block, Mapping):
        return None, ""
    total = positive_int(block.get("total"))
    if total is not None:
        return total, f"safetensors.total={total}"
    per_dtype = block.get("parameters")
    if isinstance(per_dtype, Mapping):
        counts = [positive_int(value) for value in per_dtype.values()]
        if counts and all(count is not None for count in counts):
            summed = sum(count for count in counts if count is not None)
            return summed, f"sum(safetensors.parameters)={summed} {dict(per_dtype)!r}"
    return None, ""


def metadata_facts(
    resolution: RepoResolution,
    *,
    config: Mapping[str, Any] | None = None,
    observed_at: str | None = None,
) -> dict[str, Fact[Any]]:
    """``released_at`` / ``parameters`` facts from one resolved repo's metadata.

    Args:
        resolution: The resolved repo (its ``metadata`` is the public metadata
            call at ``resolution.sha``).
        config: The repo's full ``config.json`` at the pinned commit, when the
            caller already read it; the metadata's own ``config`` subset otherwise.
        observed_at: Evidence time; now when omitted.

    Returns:
        Only the facts the metadata states (an empty dict for a repo that
        states neither).
    """
    observed_at = observed_at or _now_iso()
    meta = resolution.metadata
    where = f"{resolution.repo}@{resolution.sha[:12]}"
    if resolution.repo != resolution.requested:
        where += f" (via cardData.base_model of {resolution.requested})"
    facts: dict[str, Fact[Any]] = {}

    created = meta.get("createdAt")
    released = release_from_text(created)
    if released is not None:
        facts["released_at"] = Fact(
            released, "hf_repo", observed_at, f"huggingface {where}: createdAt={created!r}"
        )

    total, total_detail = safetensors_total(meta)
    config_source = "config.json" if isinstance(config, Mapping) else "metadata config"
    experts_total, experts_active, experts_detail = experts_from_config(
        config if isinstance(config, Mapping) else meta.get("config")
    )
    count = ParameterCount(total=total, experts_total=experts_total, experts_active=experts_active)
    if count.known:
        stated = [total_detail] if total_detail else []
        if experts_detail:
            stated.append(f"{config_source} {experts_detail}")
        facts["parameters"] = Fact(
            count, "hf_repo", observed_at, f"huggingface {where}: {' '.join(stated)}"
        )
    return facts


class HfMetadataSource:
    """A metadata-only Hub layer for a repo some source LINKED (never guessed).

    Implements :class:`~clio_agent.providers.capabilities.model_sources.HfRepoSource`.
    """

    def __init__(self, repo_id: str, *, allow_fetch: bool = True) -> None:
        self._repo_id = repo_id
        self._allow_fetch = allow_fetch

    def facts(self, model_key: str) -> ModelCapabilities | None:
        """The linked repo's ``released_at`` / ``parameters``, or ``None``."""
        from clio_agent.providers.capabilities.hf_repo import resolve_repo  # noqa: PLC0415

        resolution = resolve_repo(self._repo_id, allow_fetch=self._allow_fetch)
        if resolution is None:
            return None
        facts = metadata_facts(resolution)
        if not facts:
            return None
        return ModelCapabilities(
            model_key=model_key,
            released_at=facts.get("released_at", unknown()),
            parameters=facts.get("parameters", unknown()),
        )


__all__ = ["HfMetadataSource", "metadata_facts", "safetensors_total"]
