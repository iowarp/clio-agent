"""Fleet accounts and entries, projected from the provider catalog snapshot.

The fleet is a read model over what CLIO already stores (design:
``docs/design/sdk/model-fleet.md``). An *account* is one configured provider
with its credential; an *entry* is one ``(account, model)`` pair carrying the
capability facts a :class:`~clio_agent.sdk.model.Requires` is checked against,
its price, and whether it can be used now. Nothing here probes or caches: the
catalog snapshot (``provider_catalog_snapshot.read_catalog``) is the source.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from clio_agent.providers.catalog import get_provider

#: Catalog ``auth`` states that mean the account's credential is in place.
_SIGNED_IN = frozenset({"ok", "not_required"})


@dataclass(frozen=True)
class FleetAccount:
    """One connected provider account."""

    id: str
    label: str
    provider_kind: str
    billing: str
    health: str
    failure: str = ""
    endpoint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FleetEntry:
    """One model an account serves, with the facts selection checks."""

    account: str
    model: str
    aliases: tuple[str, ...] = ()
    #: ``None`` means unknown: a requirement on it is unmet with that reason.
    tool_calling: bool | None = None
    vision: bool | None = None
    context_tokens: int | None = None
    output_tokens: int | None = None
    effort_levels: tuple[str, ...] = ()
    #: ``{"input": {"kind", "per_1m"}, "output": {...}}`` from the catalog's pricing fact.
    price: dict[str, Any] = field(default_factory=dict)
    price_source: str = ""
    available: bool = False
    unavailable_reason: str = ""

    @property
    def ref(self) -> str:
        """The exact ``account/model`` reference."""

        return f"{self.account}/{self.model}"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ref": self.ref}


def _account(row: dict[str, Any]) -> FleetAccount | None:
    """``row`` as an account, or ``None`` when it is not connected.

    A provider is connected when its credential is in place and it either
    answered its probe or needs a credential at all. A local runtime that needs
    no credential and is not running (LM Studio on a laptop that never started
    it) is not an account; it appears once it answers.
    """

    provider_id = str(row.get("id") or "")
    auth = str(row.get("auth") or "")
    health = str(row.get("health") or "")
    if not provider_id or auth not in _SIGNED_IN:
        return None
    if auth == "not_required" and health != "ready":
        return None
    preset = get_provider(provider_id)
    return FleetAccount(
        id=provider_id,
        label=str(row.get("name") or provider_id),
        provider_kind=str(row.get("kind") or ""),
        billing=preset.billing if preset is not None else "metered",
        health=health,
        failure=str(row.get("failure") or ""),
        endpoint=str(row.get("endpoint") or ""),
    )


def _capability_values(model: dict[str, Any]) -> set[str]:
    tags = model.get("capability_tags")
    rows = tags.get("capabilities") if isinstance(tags, dict) else None
    return {str(item.get("value")) for item in rows or [] if isinstance(item, dict)}


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _price(model: dict[str, Any]) -> tuple[dict[str, Any], str]:
    facts = model.get("model_facts")
    pricing = facts.get("pricing") if isinstance(facts, dict) else None
    if not isinstance(pricing, dict) or not isinstance(pricing.get("value"), dict):
        return {}, ""
    value = pricing["value"]
    evidence = pricing.get("evidence") or []
    source = str(evidence[0].get("source") or "") if evidence else ""
    return {"input": value.get("input"), "output": value.get("output")}, source


def _entry(account: FleetAccount, model: dict[str, Any]) -> FleetEntry:
    capabilities = _capability_values(model)
    modalities = {str(item) for item in model.get("modalities") or []}
    reasoning = model.get("reasoning") if isinstance(model.get("reasoning"), dict) else {}
    price, price_source = _price(model)
    availability = str(model.get("availability") or "")
    if account.health != "ready":
        reason = f"account_{account.health or 'unknown'}"
    elif availability != "available":
        reason = f"model_{availability or 'unknown'}"
    else:
        reason = ""
    return FleetEntry(
        account=account.id,
        model=str(model.get("model_id") or ""),
        aliases=tuple(str(alias) for alias in model.get("aliases") or []),
        tool_calling=bool(model.get("native_tool_calling")) or "tool_calling" in capabilities,
        vision="image" in modalities if modalities else None,
        context_tokens=_int(model.get("loaded_context_window"))
        or _int(model.get("context_window")),
        output_tokens=_int(model.get("output_limit")),
        effort_levels=tuple(str(level) for level in reasoning.get("levels") or []),
        price=price,
        price_source=price_source,
        available=not reason,
        unavailable_reason=reason,
    )


def project_fleet(catalog: dict[str, Any]) -> tuple[list[FleetAccount], list[FleetEntry]]:
    """The connected accounts in ``catalog`` and the chat models each serves."""

    accounts: list[FleetAccount] = []
    entries: list[FleetEntry] = []
    for row in catalog.get("providers") or []:
        if not isinstance(row, dict):
            continue
        account = _account(row)
        if account is None:
            continue
        accounts.append(account)
        for model in row.get("models") or []:
            if isinstance(model, dict) and model.get("chat_selectable") and model.get("model_id"):
                entries.append(_entry(account, model))
    return accounts, entries


__all__ = ["FleetAccount", "FleetEntry", "project_fleet"]
