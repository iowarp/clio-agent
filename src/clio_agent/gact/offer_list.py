"""The plugins the agent may offer: the person's choice over the default list (OL-D1 to OL-D7).

The session facts name the plugins and connectors a conversation could take
(:func:`clio_agent.gact.plugin_offer.offerable`), the first ``plugins.offer_listed`` of
them by default. The person can change that list in Settings ("Plugins the agent can
offer"): untick an entry inside the default cap, or tick one beyond it. Their choice is
an overlay on the default, never a copy of the list, so entries that arrive later
(a new marketplace, a connector) still follow the default order:

* ``exclude``: entries the person unticked. The session facts and ``offer_plugin``
  with no plugin leave them out; naming one explicitly still offers it.
* ``include``: entries the person ticked beyond the default cap.

The listed set is (the first ``offer_listed`` candidates, minus ``exclude``) plus
``include``, in candidate order. Entries are keyed ``connector:<id>`` or
``plugin:<marketplace>/<id>``; a key no candidate carries is ignored when read
(entries come and go) and dropped when saved.

The overlay lives in the person's user ``config.yaml`` under ``plugins.offer``, next to
``plugins.offer_listed`` (the shared :mod:`clio_agent.user_config_document` owner and
its lock). It applies from the next turn: the session facts are composed at turn start.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from clio_agent import conf
from clio_agent.runtime import trace
from clio_agent.user_config_document import (
    USER_CONFIG_LOCK,
    read_document,
    user_config_path,
    write_document,
)

#: Where the overlay is kept in the user ``config.yaml``.
SECTION, KEY = "plugins", "offer"
#: Typed: the user configuration could not be read, so the default list applies.
OVERLAY_UNREADABLE = "offer_list_overlay_unreadable"


class OfferListError(ValueError):
    """The overlay could not be read or written, or a request is invalid."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Overlay:
    """The person's changes to the default list."""

    include: frozenset[str] = frozenset()
    exclude: frozenset[str] = frozenset()

    @property
    def empty(self) -> bool:
        return not (self.include or self.exclude)


def entry_key(row: Mapping[str, Any]) -> str:
    """``connector:<id>`` or ``plugin:<marketplace>/<id>`` for a candidate row."""

    if row.get("kind") == "connector":
        return f"connector:{row['id']}"
    return f"plugin:{row.get('ref') or row['id']}"


def _keys(raw: Any) -> frozenset[str]:
    if not isinstance(raw, (list, tuple)):
        return frozenset()
    return frozenset(str(item) for item in raw if isinstance(item, str) and item)


def read_overlay() -> Overlay:
    """The stored overlay (empty when none). Raises :class:`OfferListError` when the
    user configuration cannot be read."""

    try:
        document = read_document(user_config_path())
    except (OSError, yaml.YAMLError, ValueError) as exc:
        message = f"could not read the user config: {exc}"
        raise OfferListError(OVERLAY_UNREADABLE, message, 409) from exc
    section = document.get(SECTION)
    raw = section.get(KEY) if isinstance(section, Mapping) else None
    if not isinstance(raw, Mapping):
        return Overlay()
    return Overlay(include=_keys(raw.get("include")), exclude=_keys(raw.get("exclude")))


def overlay_or_default() -> Overlay:
    """The overlay for composing what the agent reads; the default list, traced with
    :data:`OVERLAY_UNREADABLE`, when it cannot be read."""

    try:
        return read_overlay()
    except OfferListError as exc:
        trace.event("TOOLS", "%s (%s)", OVERLAY_UNREADABLE, exc)
        return Overlay()


def _store(overlay: Overlay) -> None:
    with USER_CONFIG_LOCK:
        path = user_config_path()
        try:
            document = read_document(path)
        except (OSError, yaml.YAMLError, ValueError) as exc:
            message = f"could not read the user config: {exc}"
            raise OfferListError(OVERLAY_UNREADABLE, message, 409) from exc
        section = dict(document.get(SECTION) or {})
        if overlay.empty:
            section.pop(KEY, None)
        else:
            section[KEY] = {"include": sorted(overlay.include), "exclude": sorted(overlay.exclude)}
        if section:
            document[SECTION] = section
        else:
            document.pop(SECTION, None)
        try:
            write_document(path, document)
        except OSError as exc:
            raise OfferListError("offer_list_unwritable", f"could not save: {exc}", 500) from exc
        conf.reload()


# ------------------------------------------------------------------ the list


def listed(rows: Sequence[Mapping[str, Any]], cap: int, overlay: Overlay) -> list[Any]:
    """The rows the agent is told about: within ``cap`` and not excluded, or included."""

    return [
        row
        for index, row in enumerate(rows)
        if (key := entry_key(row)) in overlay.include
        or (index < cap and key not in overlay.exclude)
    ]


def not_excluded(rows: Iterable[Mapping[str, Any]], overlay: Overlay) -> list[Any]:
    """Every row the person did not untick (what ``offer_plugin`` lists with no plugin)."""

    return [row for row in rows if entry_key(row) not in overlay.exclude]


def normalized(
    rows: Sequence[Mapping[str, Any]], cap: int, include: Iterable[str], exclude: Iterable[str]
) -> Overlay:
    """The overlay for the wanted ticks: unknown keys dropped, and only real changes kept
    (an include inside the cap, or an exclude beyond it, is the default already)."""

    default = {entry_key(row) for row in rows[:cap]}
    known = {entry_key(row) for row in rows}
    wanted_in, wanted_out = set(include) & known, set(exclude) & known
    if both := wanted_in & wanted_out:
        raise OfferListError(
            "offer_list_conflict", f"ticked and unticked at once: {', '.join(sorted(both))}"
        )
    return Overlay(include=frozenset(wanted_in - default), exclude=frozenset(wanted_out & default))


# ------------------------------------------------------------------ settings view


def _logo(row: Mapping[str, Any]) -> dict[str, Any]:
    if row.get("kind") != "connector":
        return {"logo_url": None, "logo_dark_url": None}
    from clio_agent.connectors import state  # noqa: PLC0415

    found = state.all_entries().get(str(row["id"]))
    return found.logo_urls() if found is not None else {"logo_url": None, "logo_dark_url": None}


def candidates(app: Any) -> list[dict[str, Any]]:
    """The candidates for a new conversation with the person's default agent, in order.

    Raises :class:`OfferListError` (typed) when the default agent does not resolve or a
    store cannot be read.
    """

    from clio_agent.connectors.state import ConnectorStateError  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import SessionAgentUnavailable  # noqa: PLC0415
    from clio_agent.gact.plugin_offer import default_offerable  # noqa: PLC0415
    from clio_agent.plugins.store import StoreError  # noqa: PLC0415

    try:
        return default_offerable(app)
    except SessionAgentUnavailable as exc:
        raise OfferListError(exc.reason, str(exc), 409) from exc
    except (StoreError, ConnectorStateError) as exc:
        raise OfferListError("offer_list_store_unreadable", str(exc), 409) from exc


def view(app: Any) -> dict[str, Any]:
    """Every candidate (:func:`candidates`), with whether it is in the default list and
    in the effective one, and the cap."""

    from clio_agent.gact.plugin_offer import offer_listed  # noqa: PLC0415

    rows = candidates(app)
    cap = offer_listed()
    stored = read_overlay()
    chosen = normalized(rows, cap, stored.include, stored.exclude)
    shown = {entry_key(row) for row in listed(rows, cap, chosen)}
    entries = []
    for index, row in enumerate(rows):
        key = entry_key(row)
        entries.append(
            {
                "key": key,
                "kind": row["kind"],
                "id": row["id"],
                "name": row["name"],
                "summary": row.get("summary") or "",
                **({"marketplace": row["marketplace"]} if row.get("marketplace") else {}),
                **({"enabled": bool(row.get("enabled"))} if row["kind"] == "connector" else {}),
                **_logo(row),
                "default_listed": index < cap,
                "listed": key in shown,
            }
        )
    return {
        "cap": cap,
        "total": len(rows),
        "listed_count": len(shown),
        "customized": not chosen.empty,
        "include": sorted(chosen.include),
        "exclude": sorted(chosen.exclude),
        "entries": entries,
    }


def update(app: Any, include: Iterable[str], exclude: Iterable[str]) -> dict[str, Any]:
    """Save the wanted ticks over the default list; returns the new :func:`view`."""

    from clio_agent.gact.plugin_offer import offer_listed  # noqa: PLC0415

    _store(normalized(candidates(app), offer_listed(), include, exclude))
    return view(app)


def reset(app: Any) -> dict[str, Any]:
    """Clear the overlay: the default list applies again."""

    _store(Overlay())
    return view(app)
