"""The spending guard: which accounts Clio may run delegated work on without asking.

Settings carry one switch per account, **CLIO may use this account for
delegation** (``fleet.delegation`` in the user configuration). It is on by
default for subscriptions and free (local and site) accounts and off for
metered and credit accounts, so an agent does not spend OpenRouter credit or
an API key's balance the person did not mean to spend. When Clio chooses a
model (explicitly or from a preference) on an account that is off, and the
account is not the one the conversation already runs on, the person is asked
once per conversation; the answer is remembered on the conversation's root
session.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from clio_agent import conf
from clio_agent.providers.catalog import get_provider
from clio_agent.user_config_document import (
    USER_CONFIG_LOCK,
    read_document,
    user_config_path,
    write_document,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

#: Billing kinds Clio may delegate to unless the person turned the account off.
ALLOWED_BY_DEFAULT = frozenset({"subscription", "free"})
#: Root-session metadata key: accounts the person allowed for this conversation.
CONSENT_KEY = "fleet_spending_consent"


def _overrides() -> dict[str, bool]:
    value = conf.resolve("fleet.delegation", env="CLIO_FLEET_DELEGATION", default={})
    if not isinstance(value, dict):
        raise ValueError("fleet.delegation must map account ids to true or false")
    return {str(key): bool(allowed) for key, allowed in value.items()}


def delegation_allowed(account: str, billing: str = "") -> bool:
    """Whether Clio may run delegated work on ``account`` without asking."""

    if not billing:
        preset = get_provider(account)
        billing = preset.billing if preset is not None else "metered"
    return _overrides().get(account, billing in ALLOWED_BY_DEFAULT)


def set_delegation_allowed(account: str, allowed: bool) -> None:
    """Save the switch for ``account`` in the user configuration.

    Raises:
        ValueError: ``account`` is not a provider CLIO knows.
    """

    if get_provider(account) is None:
        raise ValueError(f"unknown account {account!r}")
    with USER_CONFIG_LOCK:
        path = user_config_path()
        document = read_document(path)
        fleet = dict(document.get("fleet") or {})
        delegation = dict(fleet.get("delegation") or {})
        delegation[account] = bool(allowed)
        fleet["delegation"] = delegation
        document["fleet"] = fleet
        write_document(path, document)
        conf.reload()


def _root(app: "FastAPI", sid: str) -> Any:
    sessions = app.state.sessions
    current, seen = sessions.get(sid), {sid}
    while (
        current is not None and current.parent_session_id and current.parent_session_id not in seen
    ):
        seen.add(current.parent_session_id)
        parent = sessions.get(current.parent_session_id)
        if parent is None:
            break
        current = parent
    return current


def _ask(app: "FastAPI", sid: str, account: str, model: str) -> str:
    gate = app.state.make_permission_gate()
    preset = get_provider(account)
    label = preset.label if preset is not None else account
    return gate(
        "use_model_account",
        {"account": account, "model": model},
        {
            "kind": "native",
            "summary": (
                f"Run delegated work on {model}, billed to your {label} account. "
                "Clio asks once per conversation for accounts that cost money."
            ),
            "always_ask": True,
        },
    )


async def guard_spending(
    app: "FastAPI", caller_sid: str, choice: dict[str, Any]
) -> dict[str, Any] | None:
    """``None`` when the chosen model may run, else the typed refusal.

    Only a model Clio chose on another account than the caller's is guarded: an
    inherited model is the one the person already runs the conversation on.
    """

    if choice.get("source") not in {"explicit", "prefers"}:
        return None
    ran = dict(choice.get("ran") or {})
    account, model_id = str(ran.get("provider_id") or ""), str(ran.get("model_id") or "")
    caller = app.state.sessions.get(caller_sid)
    if not account or account == str((getattr(caller, "model", None) or {}).get("provider_id")):
        return None
    if delegation_allowed(account):
        return None
    root = _root(app, caller_sid)
    consented = list((getattr(root, "metadata", None) or {}).get(CONSENT_KEY) or [])
    if account in consented:
        return None
    decision = await asyncio.to_thread(_ask, app, caller_sid, account, f"{account}/{model_id}")
    if str(decision) != "allow":
        return {
            "accepted": False,
            "reason": "model_spending_not_allowed",
            "error": (
                f"the person did not allow delegated work on the {account} account "
                f"({decision}); choose a model on an account Clio may use (list_models)"
            ),
        }
    if root is not None:
        app.state.sessions.update(root.id, metadata_patch={CONSENT_KEY: [*consented, account]})
    return None


__all__ = [
    "ALLOWED_BY_DEFAULT",
    "CONSENT_KEY",
    "delegation_allowed",
    "guard_spending",
    "set_delegation_allowed",
]
