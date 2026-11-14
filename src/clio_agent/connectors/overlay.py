"""A conversation's own connector values (CV-D1 to CV-D3).

The values a person or the agent gives for a connector's ``address`` and ``value``
needs inside one conversation belong to that conversation: they are kept in the
session's metadata under :data:`KEY` as ``{connector id: {need key: value}}`` and laid
over the person's defaults (``connectors.yaml``, :mod:`.state`) only there. Secrets
and sign-ins are never in it: they stay in CLIO's credential store, for every
conversation (CN8).

Session metadata is sent to the client in full and the client can patch it, so an
overlay is **checked again every time it is read** (:func:`validated`): an unknown
connector or need, a secret or a sign-in, a value that is not one plain line, holds
``${``, starts an address with ``-`` or is not one of a picker's options is refused
with the typed reason :data:`OVERLAY_VALUE_REFUSED` and never reaches a server's
command, arguments, environment or address.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

#: The session-metadata key that holds a conversation's connector values.
KEY = "connector_values"
#: Typed trace reason: a stored overlay value failed the read-time check and was dropped.
OVERLAY_VALUE_REFUSED = "connector_overlay_value_refused"
#: Need kinds a conversation may set; secrets and sign-ins are the person's, everywhere.
OVERLAY_KINDS = frozenset({"address", "value"})
MAX_VALUE = 4096

Overlay = dict[str, dict[str, str]]


def value_problem(need: Any, value: str) -> str | None:
    """Why ``value`` cannot be ``need``'s value (``None`` when it can).

    The one check for a value the person or the agent gives, wherever it is stored.
    """

    if "\n" in value or "\r" in value or "\x00" in value:
        return "one plain line"
    if "${" in value:
        return "no ${...} references"
    if len(value) > MAX_VALUE:
        return f"at most {MAX_VALUE} characters"
    if need.kind == "address" and value.startswith("-"):
        return "an address cannot start with '-'"
    if need.options and value not in need.options:
        return f"one of {', '.join(need.options)}"
    return None


def validated(raw: Any, entries: Mapping[str, Any], *, where: str = "") -> Overlay:
    """The checked overlay in ``raw`` (a session's ``connector_values``).

    Every refused item is traced with :data:`OVERLAY_VALUE_REFUSED`, the connector, the
    key and the reason; never the value.
    """

    from clio_agent.runtime import trace  # noqa: PLC0415

    def refuse(connector: str, key: str, reason: str) -> None:
        trace.event(
            "TOOLS",
            "%s session=%s connector=%s key=%s reason=%s",
            OVERLAY_VALUE_REFUSED,
            where or "-",
            connector[:80],
            key[:80],
            reason,
        )

    out: Overlay = {}
    if raw in (None, {}):
        return out
    if not isinstance(raw, Mapping):
        refuse("*", "*", "not_a_mapping")
        return out
    for connector_id, values in raw.items():
        entry = entries.get(str(connector_id))
        if entry is None:
            refuse(str(connector_id), "*", "unknown_connector")
            continue
        if not isinstance(values, Mapping):
            refuse(entry.id, "*", "not_a_mapping")
            continue
        for key, value in values.items():
            need = entry.need(str(key))
            if need is None:
                refuse(entry.id, str(key), "unknown_need")
            elif need.kind not in OVERLAY_KINDS:
                refuse(entry.id, need.key, f"{need.kind}_not_per_conversation")
            elif not isinstance(value, str) or not value.strip():
                refuse(entry.id, need.key, "not_a_string")
            elif problem := value_problem(need, value.strip()):
                refuse(entry.id, need.key, problem.replace(" ", "_"))
            else:
                out.setdefault(entry.id, {})[need.key] = value.strip()
    return out


def merged(overlay: Mapping[str, str], changes: Mapping[str, str | None]) -> dict:
    """``overlay`` for one connector with ``changes`` applied (``None`` or "" clears a key)."""

    out = dict(overlay)
    for key, value in changes.items():
        if value in (None, ""):
            out.pop(key, None)
        else:
            out[key] = str(value)
    return out


def digest(overlay: Mapping[str, Mapping[str, str]]) -> str:
    """A short stable name for ``overlay`` ("" when it holds nothing)."""

    kept = {cid: dict(values) for cid, values in overlay.items() if values}
    if not kept:
        return ""
    blob = json.dumps(kept, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
