"""The fast model: what CLIO's helper work runs on when its fleet role names no model.

Titles, summaries, judges, the explore subagent, the search assistant, SPOTTER and the
memory pass are small calls. When the person has not given one of those roles its own
model, it runs on the fast model, resolved in this order:

1. ``fleet.fast`` (Settings, Models, Fast model): an explicit account and model;
2. else the caller's account's fast family (the provider's ``fast_model_family``,
   Haiku for Claude Code), its first available model in catalog order (newest first);
3. else none, and the helper runs on the caller's model as before.

A role set to a model always runs that model; picking the conversation model there
keeps a helper on it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.gact.fleet.entries import FleetEntry

_LOG = logging.getLogger(__name__)


def configured_fast() -> dict[str, str] | None:
    """The person's ``fleet.fast`` choice, ``{provider_id, model_id}``, or ``None``."""
    from clio_agent.gact.fleet_settings import fleet_settings  # noqa: PLC0415

    return fleet_settings().get("fast")


def account_fast_ref(entries: list[FleetEntry], account: str) -> str:
    """``account``'s fast model as ``account/model`` (its provider's fast family), or "".

    The first available model of that family in catalog order, which lists the newest
    first.
    """
    from clio_agent.gact.fleet.entries import account_provider  # noqa: PLC0415

    provider = account_provider(account) if account else None
    family = getattr(provider, "fast_model_family", "") if provider is not None else ""
    if not family:
        return ""
    entry = next(
        (
            e
            for e in entries
            if e.account == account
            and e.available
            and e.tool_calling is not False
            and family in e.model
        ),
        None,
    )
    return entry.ref if entry is not None else ""


def fast_ref(entries: list[FleetEntry], caller_account: str) -> tuple[str, str]:
    """``(account/model, why)`` of the fast model for a caller, or ``("", "")``."""
    explicit = configured_fast()
    if explicit:
        return f"{explicit['provider_id']}/{explicit['model_id']}", "the fast model"
    ref = account_fast_ref(entries, caller_account)
    return (ref, "the account's fast model") if ref else ("", "")


def caller_account(lm: Any) -> str:
    """The fleet account an LM runs on (a saved model server is its own account)."""
    config = getattr(lm, "_clio_provider_config", None) or lm
    return str(
        getattr(config, "server_id", "")
        or getattr(config, "provider_id", "")
        or getattr(config, "provider", "")
        or ""
    )


def fast_identity(caller: Any = None, app: Any = None, account: str = "") -> dict[str, str] | None:
    """``{provider_id, model_id, source}`` of the fast model, or ``None`` when there is none.

    ``caller`` is the LM the work would otherwise run on, or ``account`` the account it
    runs on; with neither, the server's running model's account supplies the fast
    family. ``source`` is ``fast`` for the person's choice and ``account_fast`` for the
    account's family.
    """
    explicit = configured_fast()
    if explicit:
        return {**explicit, "source": "fast"}
    if app is None:
        from clio_agent.gact.context import active_app  # noqa: PLC0415

        app = active_app()
    if app is None:
        return None
    from clio_agent.gact.fleet.child_model import fleet_entries  # noqa: PLC0415

    entries = fleet_entries(app, needed=False)
    if not entries:
        return None
    if caller is not None:
        account = caller_account(caller)
    elif not account:
        from clio_agent.gact.providers.config import _active_lm_model_ref  # noqa: PLC0415

        account = _active_lm_model_ref(app)["provider_id"]
    ref = account_fast_ref(entries, account)
    entry = next((e for e in entries if ref and e.ref == ref), None)
    if entry is None:
        return None
    return {"provider_id": entry.account, "model_id": entry.model, "source": "account_fast"}


def fast_route(caller: Any, adapter: Any = None) -> tuple[Any, Any]:
    """The fast model's ``(lm, adapter)`` for helper work, else the caller's own.

    A fast model that cannot be reached is logged and the caller's model runs, so a
    helper never fails because its default moved.
    """
    from clio_agent.gact.fleet_settings import ModelRouteUnavailable, model_route  # noqa: PLC0415

    try:
        identity = fast_identity(caller)
    except (ValueError, OSError) as exc:
        _LOG.warning("fast model not resolved; the caller's model runs: %s", exc)
        return caller, adapter
    if identity is None:
        return caller, adapter
    config = getattr(caller, "_clio_provider_config", None)
    if (
        caller_account(caller) == identity["provider_id"]
        and str(getattr(config, "model", "") or getattr(caller, "model", ""))
        == identity["model_id"]
    ):
        return caller, adapter
    try:
        return model_route(identity["provider_id"], identity["model_id"])
    except (ModelRouteUnavailable, ValueError, OSError) as exc:
        _LOG.warning(
            "fast model %s/%s unavailable; the caller's model runs: %s",
            identity["provider_id"],
            identity["model_id"],
            exc,
        )
        return caller, adapter
