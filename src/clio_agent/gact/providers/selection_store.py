"""Durable LM provider selection: a bind through the API survives a restart.

``PUT /v1/providers/lm`` (the model picker's route) binds the provider in
memory. Boot decides whether to build an agent from the *config file* (or
``CLIO_LM_PROVIDER``) only -- :func:`~clio_agent.gact.providers.boot_selection.
explicit_lm_provider` and :func:`clio_agent.config.load_config_from_env` -- so a
selection that lived only in memory was lost on every restart and the service
came back "unconfigured" (A2 follow-up, 2026-09-27).

This module writes the bound selection into the user config file's ``lm``
section, the one layer boot already reads; no new store. Only the selection's
identity is written:

* ``lm.provider`` -- the provider **preset id** (``claude_code``,
  ``argonne_metis``, ``vllm``); ``LMProviderConfig`` resolves a preset id before
  a provider kind, so the exact preset (its api_base default, credential key)
  comes back;
* ``lm.model``, and ``lm.api_base`` when the bind named one (otherwise the key is
  removed so the preset default applies);
* the transport keys of the bound provider (``lm.claude_code_transport`` /
  ``lm.codex_transport``); a previous provider's transport keys are removed.

A removed key (:data:`clio_agent.removed_config_keys.REMOVED_CONFIG_KEYS`) still set anywhere is
never rewritten around: the write is refused with the typed reason below and the
plain-language detail naming what to delete.

API keys are never written: the secret tier stays out of config files, and a
saved key already survives restarts in ``ProviderApiKeyStore``. A failure to
persist is returned as a typed reason, never swallowed.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from clio_agent import user_config_document
from clio_agent.user_config_document import USER_CONFIG_LOCK as _LOCK
from clio_agent.user_config_document import read_document as _read_document
from clio_agent.user_config_document import write_document as _write_document

logger = logging.getLogger(__name__)

#: The ``lm`` keys this module owns; every other ``lm`` key is left untouched.
OWNED_LM_KEYS = (
    "provider",
    "model",
    "api_base",
    "claude_code_transport",
    "codex_transport",
)

#: Typed reason recorded when the selection could not be written.
LM_SELECTION_NOT_PERSISTED = "lm_selection_not_persisted"


@dataclass(frozen=True)
class SelectionPersistence:
    """The outcome of one persist attempt (``reason`` is empty on success)."""

    persisted: bool
    path: str
    reason: str = ""
    detail: str = ""


def user_config_path() -> Path:
    """The user config file boot reads (``<user config dir>/config.yaml``)."""

    return user_config_document.user_config_path()


def selection_entries(cfg: Any, *, requested_api_base: str) -> dict[str, Any]:
    """The ``lm`` entries that restore ``cfg`` at boot (see the module doc).

    Args:
        cfg: The bound, normalized ``LMProviderConfig``.
        requested_api_base: The api_base the bind request named (``""`` = the
            preset default, which is then not pinned in the file).
    """

    provider = str(cfg.provider)
    entries: dict[str, Any] = {
        "provider": str(cfg.provider_id or provider),
        "model": str(cfg.model or ""),
    }
    if requested_api_base.strip():
        entries["api_base"] = requested_api_base.strip()
    if provider == "claude_code":
        entries["claude_code_transport"] = str(cfg.claude_code_transport)
    elif provider == "codex":
        entries["codex_transport"] = str(cfg.codex_transport)
    return entries


def persist_lm_selection(cfg: Any, *, requested_api_base: str) -> SelectionPersistence:
    """Write the bound selection into the user config file's ``lm`` section.

    Read-modify-write under a process lock with an atomic replace: unrelated
    top-level settings and non-owned ``lm`` keys are preserved. The config store
    is reloaded afterwards so this process reads what the next boot will read.

    Returns:
        :class:`SelectionPersistence`; on failure ``reason`` is
        :data:`LM_SELECTION_NOT_PERSISTED` and ``detail`` names the cause.
    """

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load
    from clio_agent.removed_config_keys import reject_removed_config_keys  # noqa: PLC0415

    path = user_config_path()
    entries = selection_entries(cfg, requested_api_base=requested_api_base)
    try:
        with _LOCK:
            reject_removed_config_keys()  # RemovedConfigKeyError is a ValueError
            document = _read_document(path)
            current = document.get("lm")
            lm = dict(current) if isinstance(current, Mapping) else {}
            for key in OWNED_LM_KEYS:
                lm.pop(key, None)
            lm.update(entries)
            document["lm"] = lm
            _write_document(path, document)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.warning(
            "⚑ LM-SELECTION reason=%s path=%s detail=%s",
            LM_SELECTION_NOT_PERSISTED,
            path,
            exc,
        )
        return SelectionPersistence(
            persisted=False, path=str(path), reason=LM_SELECTION_NOT_PERSISTED, detail=str(exc)
        )
    conf.reload()
    return SelectionPersistence(persisted=True, path=str(path))


def selection_status_fields(app: Any) -> dict[str, Any]:
    """The ``lm_config_status`` fields that say whether the bind survives a restart.

    ``selection_persisted`` is always present after a bind; a failed write adds
    the typed ``selection_persist_reason`` and its ``selection_persist_detail``.
    """

    outcome = getattr(app.state, "lm_selection_persistence", None)
    if not isinstance(outcome, SelectionPersistence):
        return {}
    fields: dict[str, Any] = {"selection_persisted": outcome.persisted}
    if not outcome.persisted:
        fields["selection_persist_reason"] = outcome.reason
        fields["selection_persist_detail"] = outcome.detail
        # ``message`` is what GET /v1/providers/lm serves as ``status_message``: the
        # bind works now, but the person is told it will not survive a restart.
        fields["message"] = (
            f"LM provider ready, but not saved for the next restart ({outcome.reason}: "
            f"{outcome.detail})"
        )
    return fields


__all__ = [
    "LM_SELECTION_NOT_PERSISTED",
    "OWNED_LM_KEYS",
    "SelectionPersistence",
    "persist_lm_selection",
    "selection_entries",
    "selection_status_fields",
    "user_config_path",
]
