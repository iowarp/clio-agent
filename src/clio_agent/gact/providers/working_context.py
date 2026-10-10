"""CLIO's working context for a model it cannot configure (model semantics).

A cloud model, or a server someone else runs, has a context CLIO cannot set.
What CLIO can choose is how much of it to use: the working context the agent
loop budgets against -- the denominator of auto-compaction
(:func:`clio_agent.gact.runtime.context_tokens._resolve_expert_context_window`)
and the ``chosen_context`` a bind reports. "Max" (the default) uses the model's
reported maximum; a typed number uses less, never more (it is bounded by the
reported maximum when one is known). There is no Fit to GPU: CLIO does not
size another party's server.

The choice is persisted per provider and model in the EXISTING user
``config.yaml`` under ``providers.working_context`` (no new store), as
``{provider, model, tokens}`` rows; "Max" is the absence of a row. The session
model reference carries no context field, so there is no per-session override.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from clio_agent.context_sizing.controls import (
    ContextControls,
    unavailable_fit,
)
from clio_agent.user_config_document import USER_CONFIG_LOCK as _LOCK
from clio_agent.user_config_document import read_document, user_config_path, write_document

logger = logging.getLogger(__name__)

_SECTION = ("providers", "working_context")
#: The smallest working context a person may choose.
MINIMUM_WORKING_CONTEXT = 1024
#: ``(path, mtime_ns, size) -> {(provider, model): tokens}``: one read per file change.
_CACHE: dict[tuple[str, int, int], dict[tuple[str, str], int]] = {}

FIT_UNAVAILABLE = (
    "CLIO does not run this model's server, so it cannot size the server's context to a GPU"
)


class WorkingContextStoreError(ValueError):
    """The saved working contexts could not be read, validated or written."""


def _rows(document: Mapping[str, Any]) -> dict[tuple[str, str], int]:
    providers = document.get(_SECTION[0])
    raw = providers.get(_SECTION[1]) if isinstance(providers, Mapping) else None
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise WorkingContextStoreError("providers.working_context must be a list")
    rows: dict[tuple[str, str], int] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise WorkingContextStoreError("each providers.working_context entry must be a mapping")
        tokens = item.get("tokens")
        if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens <= 0:
            raise WorkingContextStoreError("providers.working_context tokens must be positive")
        rows[(str(item.get("provider") or ""), str(item.get("model") or ""))] = tokens
    return rows


def saved_working_contexts() -> dict[tuple[str, str], int]:
    """Every saved working context by ``(provider, model)``.

    An unreadable file is logged and treated as no saved choice: the agent
    loop then budgets against the model's maximum, never fails a turn.
    """

    path = user_config_path()
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    try:
        with _LOCK:
            rows = _rows(read_document(path))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.warning("working context not read: reason=unreadable path=%s detail=%s", path, exc)
        return {}
    _CACHE.clear()
    _CACHE[key] = rows
    return rows


def saved_working_context(provider_id: str, model: str) -> int | None:
    """The saved working context of ``model`` on ``provider_id``, or None ("Max")."""

    return saved_working_contexts().get((provider_id, model))


def save_working_context(provider_id: str, model: str, tokens: int | None) -> None:
    """Save (or with None, clear back to "Max") one model's working context.

    Raises:
        WorkingContextStoreError: For a value below the minimum or a file that
            cannot be read or written.
    """

    if not provider_id or not model:
        raise WorkingContextStoreError("a provider and a model are required")
    if tokens is not None and tokens < MINIMUM_WORKING_CONTEXT:
        raise WorkingContextStoreError(
            f"a working context must be at least {MINIMUM_WORKING_CONTEXT} tokens"
        )
    from clio_agent import conf  # noqa: PLC0415 - avoid an import cycle at module load

    path: Path = user_config_path()
    try:
        with _LOCK:
            document = read_document(path)
            rows = _rows(document)
            rows.pop((provider_id, model), None)
            if tokens is not None:
                rows[(provider_id, model)] = int(tokens)
            providers = document.get(_SECTION[0])
            section = dict(providers) if isinstance(providers, Mapping) else {}
            section[_SECTION[1]] = [
                {"provider": provider, "model": name, "tokens": value}
                for (provider, name), value in rows.items()
            ]
            document[_SECTION[0]] = section
            write_document(path, document)
    except (OSError, yaml.YAMLError) as exc:
        raise WorkingContextStoreError(f"could not save the working context: {exc}") from exc
    _CACHE.clear()
    conf.reload()


def _config_identity(cfg: Any) -> tuple[str, str]:
    provider = str(getattr(cfg, "provider_id", "") or getattr(cfg, "provider", "") or "")
    return provider, str(getattr(cfg, "model", "") or "")


def bounded(saved: int, maximum: int | None) -> int:
    """``saved`` within the model's reported maximum, when one is known."""

    return min(saved, int(maximum)) if maximum else saved


def working_context_for(cfg: Any) -> int | None:
    """The saved working context for a bound configuration, bounded by its maximum.

    The maximum is what the deployment serves (``context_window``), else the
    model's own (``native_context_window``). None when nothing is saved.
    """

    provider, model = _config_identity(cfg)
    if not provider or not model:
        return None
    saved = saved_working_context(provider, model)
    if not saved:
        return None
    maximum = getattr(cfg, "context_window", None) or getattr(cfg, "native_context_window", None)
    return bounded(saved, maximum if isinstance(maximum, int) else None)


def model_context_controls(
    provider_id: str,
    model: str,
    *,
    maximum: int | None,
    maximum_reason: str = "",
    saved: Mapping[tuple[str, str], int] | None = None,
) -> ContextControls:
    """The context control of one model CLIO binds but does not run.

    Args:
        provider_id: The provider preset id.
        model: The model id.
        maximum: The reported maximum context (served, else the model's own).
        maximum_reason: Where ``maximum`` comes from.
        saved: Pre-read :func:`saved_working_contexts` (a catalog renders many rows).
    """

    rows = saved if saved is not None else saved_working_contexts()
    choice = rows.get((provider_id, model))
    if choice:
        current = bounded(choice, maximum)
        reason = f"set by you ({choice})"
        if current < choice:
            reason += f", bounded by the model's maximum {maximum}"
        return ContextControls(
            semantics="model",
            maximum=maximum,
            maximum_reason=maximum_reason,
            minimum=MINIMUM_WORKING_CONTEXT,
            current=current,
            current_choice="number",
            current_reason=reason,
            fit_to_gpu=unavailable_fit(FIT_UNAVAILABLE),
        )
    return ContextControls(
        semantics="model",
        maximum=maximum,
        maximum_reason=maximum_reason,
        minimum=MINIMUM_WORKING_CONTEXT,
        current=maximum,
        current_choice="max",
        current_reason="the model's maximum" if maximum else "the model's maximum is not known",
        fit_to_gpu=unavailable_fit(FIT_UNAVAILABLE),
    )


__all__ = [
    "MINIMUM_WORKING_CONTEXT",
    "WorkingContextStoreError",
    "bounded",
    "model_context_controls",
    "save_working_context",
    "saved_working_context",
    "saved_working_contexts",
    "working_context_for",
]
