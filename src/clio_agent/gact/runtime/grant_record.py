"""A typed view over one ``permission_policies`` row (split from :mod:`.grant_resolver`).

The ``kind`` constants live in :mod:`.grant_resolver`, which re-exports this class; they are
read when a method runs, so importing either module first works.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from clio_agent.gact.runtime.permission_actions import POLICY_ALLOWING

__all__ = ["GrantRecord"]


def _resolver() -> Any:
    from clio_agent.gact.runtime import grant_resolver  # noqa: PLC0415 - see the module doc

    return grant_resolver


@dataclass(frozen=True)
class GrantRecord:
    """A typed view over one ``permission_policies`` row, discriminated by ``kind``.

    The store holds all three subject encodings in one list, so a grant is a ``kind`` view over
    existing fields — no data migration. :meth:`from_policy_row` synthesizes ``kind`` from whichever
    pattern field is set for a legacy row that lacks it; :meth:`to_policy_row` round-trips back.
    ``decision`` is the coarse ``allow``/``deny``/``ask`` value (the raw ``allow_session`` /
    ``allow_workspace`` stickiness is carried by ``scope``); enforcement always reads the raw store
    via :func:`resolve`, so this typed view never gates on its own. ``modes``/``on`` mirror the
    optional row-level axis fields (P0.2 #1060) as ``tuple[str, ...]`` (frozen-dataclass
    hashability); both default to empty, matching the "absent axis = matches anything" semantics
    :func:`_axis_matches` applies at enforcement.
    """

    kind: str
    pattern: str
    decision: str
    scope: str
    scope_id: str = ""
    grantor: str = ""
    created_from_permission_id: str = ""
    priority: int | None = None
    modes: tuple[str, ...] = ()
    on: tuple[str, ...] = ()

    @staticmethod
    def _kind_for_row(row: Mapping[str, Any]) -> str:
        r = _resolver()
        explicit = str(row.get("kind") or "").strip()
        if explicit in r._VALID_KINDS:
            return explicit
        if str(row.get("host_pattern") or ""):
            return r.KIND_DOMAIN
        if str(row.get("path_pattern") or ""):
            return r.KIND_ROOT
        return r.KIND_TOOL

    @classmethod
    def from_policy_row(cls, row: Mapping[str, Any]) -> "GrantRecord":
        """Build a typed grant from a stored policy row, synthesizing ``kind`` when absent."""

        r = _resolver()
        kind = cls._kind_for_row(row)
        if kind in r.HOST_KINDS:
            pattern = str(row.get("host_pattern") or "")
        elif kind == r.KIND_ROOT:
            pattern = str(row.get("path_pattern") or "")
        else:
            pattern = str(row.get("tool_name_pattern") or "*")
        action = str(row.get("action") or "").lower()
        if action in POLICY_ALLOWING:
            decision = "allow"
        elif action == "deny":
            decision = "deny"
        else:
            decision = "ask"
        raw_priority = row.get("priority")
        priority = (
            raw_priority
            if isinstance(raw_priority, int) and not isinstance(raw_priority, bool)
            else None
        )
        raw_modes = row.get("modes")
        modes = tuple(str(m) for m in raw_modes) if isinstance(raw_modes, list) else ()
        raw_on = row.get("on")
        on = tuple(str(e) for e in raw_on) if isinstance(raw_on, list) else ()
        return cls(
            kind=kind,
            pattern=pattern,
            decision=decision,
            scope=str(row.get("scope") or "").lower(),
            scope_id=str(row.get("scope_id") or ""),
            grantor=str(row.get("grantor") or ""),
            created_from_permission_id=str(row.get("created_from_permission_id") or ""),
            priority=priority,
            modes=modes,
            on=on,
        )

    def to_policy_row(self) -> dict[str, Any]:
        """Round-trip back to a ``permission_policies`` row shape (kind + subject field)."""

        r = _resolver()
        action = self.decision if self.decision in {"deny", "ask"} else "allow"
        row: dict[str, Any] = {
            "kind": self.kind,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "action": action,
        }
        if self.kind in r.HOST_KINDS:
            row["host_pattern"] = self.pattern
        elif self.kind == r.KIND_ROOT:
            row["path_pattern"] = self.pattern
        else:
            row["tool_name_pattern"] = self.pattern
        if self.grantor:
            row["grantor"] = self.grantor
        if self.created_from_permission_id:
            row["created_from_permission_id"] = self.created_from_permission_id
        if self.priority is not None:
            row["priority"] = self.priority
        if self.modes:
            row["modes"] = list(self.modes)
        if self.on:
            row["on"] = list(self.on)
        return row
