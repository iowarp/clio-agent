"""Unified grant resolver + read-only predicate for the GACT permission model (#1032).

This module is the single matcher behind the permission boundary. Historically two
near-identical loops walked ``app.state.permission_policies``: :func:`_policy_action_for_tool`
(tool glob + optional path glob, session/workspace scope) and :func:`_host_action_for`
(workspace ``host_pattern`` with the deliberate session-scope leak guard). Both now delegate
to :func:`resolve`, a single ``kind``-discriminated matcher over the SAME row shape — the store
already carries every subject encoding (``tool_name_pattern`` / ``path_pattern`` /
``host_pattern`` + ``scope`` / ``scope_id`` / ``action``), so ``kind`` is a discriminator over
existing fields, not a data migration.

Two invariants are load-bearing and preserved verbatim (blast-radius flags 2 & 3):

* :func:`resolve` returns the **RAW action vocabulary**
  (``allow``/``allow_session``/``allow_workspace``/``deny``/``ask``/``""``) — never collapsed
  to three values, because ``_append_permission_policy_from_resolution`` and the artifact
  proposal effects depend on the distinction.
* **Per-kind scope divergence.** ``kind="domain"`` honours ONLY a WORKSPACE-scoped host row
  with an EXPLICIT matching ``scope_id`` (the fleet-egress leak guard) — a session-scoped or
  empty-``scope_id`` host row is skipped. ``kind="tool"``/``"fs_root"`` honour session AND
  workspace scope, treating an empty ``scope_id`` as a wildcard for that scope type. This is
  encoded per-kind, not flattened into one uniform matcher.

:func:`is_read_only` is the structural first-branch predicate of the tool gate: a purely
POSITIVE, data-driven allowlist (MCP ``readOnlyHint`` annotation OR a static catalog ``read``
tag), with NO tool-name substring matching — that heuristic is exactly what #1032 deletes.

``mode`` is threaded through :func:`resolve` as a pass-through placeholder only; the mode enum +
``default_decision`` logic lands in #1034 (do NOT build it here).
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clio_agent.tools.catalog import get_tool_entry

#: Grant ``kind`` discriminators over the existing policy-row fields.
KIND_TOOL = "tool"
KIND_DOMAIN = "domain"
KIND_ROOT = "fs_root"

#: The full raw action vocabulary a policy row may carry (never collapsed).
_VALID_ACTIONS = frozenset({"allow", "allow_session", "allow_workspace", "deny", "ask"})

#: The gate ``context`` kind used for an external MCP tool call. Single-sourced here so the
#: permission gate + routes import one constant and :func:`is_read_only` needs no back-import.
EXTERNAL_MCP_CONTEXT_KIND = "external_mcp"

#: Standard MCP boolean hint keys. A non-boolean value for any of these makes the annotation
#: block untrustworthy, so :func:`annotations_are_read_only` requires permission (fail closed).
_MCP_BOOLEAN_HINTS: tuple[str, ...] = (
    "readOnlyHint",
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
)


def annotations_are_read_only(annotations: Any) -> bool:
    """Return whether MCP annotations explicitly and consistently declare read-only.

    MCP annotations are optional hints, so the boundary uses the one safe positive case only:
    a real boolean ``readOnlyHint=True`` with well-typed standard boolean hints and no
    contradictory ``destructiveHint=True``. Everything else (missing, malformed, contradictory)
    is NOT read-only and therefore asks for permission. Generalized from the former
    ``_external_mcp_annotations_are_read_only`` (permission_gate) so it can also classify any
    tool that declares the hint, not just the external-MCP path.
    """

    if not isinstance(annotations, Mapping):
        return False
    for name in _MCP_BOOLEAN_HINTS:
        value = annotations.get(name)
        if value is not None and type(value) is not bool:
            return False
    return annotations.get("readOnlyHint") is True and annotations.get("destructiveHint") is not True


def is_read_only(
    kind: str,
    name: str,
    args: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
) -> bool:
    """Return whether a tool call is provably read-only (the structural fast-allow).

    A POSITIVE, data-driven allowlist evaluated first-match, with NO tool-name substrings:

    1. **Annotation signal.** An external-MCP gate ``context`` (or any context) whose
       ``annotations`` declare a real boolean ``readOnlyHint=True`` (and not
       ``destructiveHint=True``) — see :func:`annotations_are_read_only`.
    2. **Catalog signal.** The static tool catalog tags the tool ``"read"`` and NOT ``"write"``.
       Native ``fs``/``shell`` tools carry NO annotation context (built-ins return ``None``), so
       this catalog consult is what classifies e.g. ``fs_read_file`` / ``fs_propose_edit``.

    Fails closed (NOT read-only → the gate proceeds to approval): ``fs_apply_edit_write`` (tagged
    ``write``), ``shell_bash`` (genuinely unclassifiable — the OS fence, not the gate, contains
    its writes/egress), and any unannotated external tool. ``args``/``kind`` are accepted for a
    stable signature (a future kind may inspect them) but are not consulted by these two signals.
    """

    _ = (kind, args)
    if isinstance(context, Mapping) and context.get("kind") == EXTERNAL_MCP_CONTEXT_KIND:
        if annotations_are_read_only(context.get("annotations")):
            return True
    entry = get_tool_entry(name)
    if entry is not None:
        tags = entry.tags
        if "read" in tags and "write" not in tags:
            return True
    return False


def _scope_matches(kind: str, policy: Mapping[str, Any], session_id: str, workspace_id: str) -> bool:
    """Return whether ``policy``'s scope admits this ``kind`` call (per-kind divergence)."""

    scope = str(policy.get("scope") or "").lower()
    scope_id = str(policy.get("scope_id") or "")
    if kind == KIND_DOMAIN:
        # Leak guard (permission_policies._host_action_for): ONLY a workspace-scoped host row
        # with an EXPLICIT matching scope_id — session-scoped and empty-scope_id rows never
        # widen an unattributable fleet-shared egress connection.
        return scope == "workspace" and scope_id == workspace_id
    # tool / fs_root: honour session AND workspace scope; empty scope_id is a wildcard for that
    # scope type (preserving _policy_action_for_tool's behaviour verbatim).
    if scope == "session":
        return not scope_id or scope_id == session_id
    if scope == "workspace":
        return not scope_id or scope_id == workspace_id
    return False


def _path_pattern_matches(path_pattern: str, path: str) -> bool:
    """Return whether ``path`` matches ``path_pattern`` (raw + resolved candidate)."""

    candidates = [path]
    if path:
        try:
            candidates.append(str(Path(path).resolve(strict=False)))
        except OSError:
            pass
    return any(fnmatch.fnmatchcase(candidate, path_pattern) for candidate in candidates)


def _subject_matches(kind: str, policy: Mapping[str, Any], pattern: str, path: str) -> bool:
    """Return whether ``policy``'s subject encoding matches this ``kind`` call."""

    if kind == KIND_DOMAIN:
        host_pattern = str(policy.get("host_pattern") or "")
        if not host_pattern:
            return False
        return fnmatch.fnmatchcase(pattern.strip().lower(), host_pattern.strip().lower())
    if kind == KIND_ROOT:
        path_pattern = str(policy.get("path_pattern") or "")
        if not path_pattern:
            return False
        return _path_pattern_matches(path_pattern, pattern)
    # kind == tool: tool glob (default "*") + optional path-glob refinement.
    tool_pattern = str(policy.get("tool_name_pattern") or "*")
    if not fnmatch.fnmatchcase(pattern, tool_pattern):
        return False
    path_pattern = str(policy.get("path_pattern") or "")
    if path_pattern and not _path_pattern_matches(path_pattern, path):
        return False
    return True


def resolve(
    kind: str,
    pattern: str,
    *,
    policies: list[Any] | None,
    session_id: str = "",
    workspace_id: str = "",
    path: str = "",
    mode: str = "",
) -> str:
    """Return the first matching policy's RAW action, or ``""`` when nothing matches.

    One matcher over ``permission_policies`` generalizing the former ``_policy_action_for_tool``
    (``kind="tool"``) and ``_host_action_for`` (``kind="domain"``). ``pattern`` is the subject
    string for the kind: the tool name (``tool``), the requested host (``domain``), or the target
    path (``fs_root``). ``path`` supplies the optional path-glob refinement a ``tool`` policy may
    additionally carry; it is unused for ``domain``/``fs_root``. ``mode`` is a pass-through
    placeholder (#1034 owns the mode enum + default_decision). Matching is first-match, returning
    the raw ``allow``/``allow_session``/``allow_workspace``/``deny``/``ask`` action verbatim.
    """

    _ = mode  # pass-through placeholder for #1034 (mode enum + default_decision)
    if not isinstance(policies, list):
        return ""
    if kind == KIND_DOMAIN and (not pattern or not workspace_id):
        # _host_action_for guard: an empty host or unknown workspace can never match a host row.
        return ""
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        if not _scope_matches(kind, policy, session_id, workspace_id):
            continue
        if not _subject_matches(kind, policy, pattern, path):
            continue
        action = str(policy.get("action") or "").lower()
        if action in _VALID_ACTIONS:
            return action
    return ""


@dataclass(frozen=True)
class GrantRecord:
    """A typed view over one ``permission_policies`` row, discriminated by ``kind``.

    The store holds all three subject encodings in one list, so a grant is a ``kind`` view over
    existing fields — no data migration. :meth:`from_policy_row` synthesizes ``kind`` from whichever
    pattern field is set for a legacy row that lacks it; :meth:`to_policy_row` round-trips back.
    ``decision`` is the coarse ``allow``/``deny``/``ask`` value (the raw ``allow_session`` /
    ``allow_workspace`` stickiness is carried by ``scope``); enforcement always reads the raw store
    via :func:`resolve`, so this typed view never gates on its own.
    """

    kind: str
    pattern: str
    decision: str
    scope: str
    scope_id: str = ""
    grantor: str = ""
    created_from_permission_id: str = ""

    @staticmethod
    def _kind_for_row(row: Mapping[str, Any]) -> str:
        explicit = str(row.get("kind") or "").strip()
        if explicit in {KIND_TOOL, KIND_DOMAIN, KIND_ROOT}:
            return explicit
        if str(row.get("host_pattern") or ""):
            return KIND_DOMAIN
        if str(row.get("path_pattern") or ""):
            return KIND_ROOT
        return KIND_TOOL

    @classmethod
    def from_policy_row(cls, row: Mapping[str, Any]) -> "GrantRecord":
        """Build a typed grant from a stored policy row, synthesizing ``kind`` when absent."""

        kind = cls._kind_for_row(row)
        if kind == KIND_DOMAIN:
            pattern = str(row.get("host_pattern") or "")
        elif kind == KIND_ROOT:
            pattern = str(row.get("path_pattern") or "")
        else:
            pattern = str(row.get("tool_name_pattern") or "*")
        action = str(row.get("action") or "").lower()
        if action in {"allow", "allow_session", "allow_workspace"}:
            decision = "allow"
        elif action == "deny":
            decision = "deny"
        else:
            decision = "ask"
        return cls(
            kind=kind,
            pattern=pattern,
            decision=decision,
            scope=str(row.get("scope") or "").lower(),
            scope_id=str(row.get("scope_id") or ""),
            grantor=str(row.get("grantor") or ""),
            created_from_permission_id=str(row.get("created_from_permission_id") or ""),
        )

    def to_policy_row(self) -> dict[str, Any]:
        """Round-trip back to a ``permission_policies`` row shape (kind + subject field)."""

        action = self.decision if self.decision in {"deny", "ask"} else "allow"
        row: dict[str, Any] = {
            "kind": self.kind,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "action": action,
        }
        if self.kind == KIND_DOMAIN:
            row["host_pattern"] = self.pattern
        elif self.kind == KIND_ROOT:
            row["path_pattern"] = self.pattern
        else:
            row["tool_name_pattern"] = self.pattern
        if self.grantor:
            row["grantor"] = self.grantor
        if self.created_from_permission_id:
            row["created_from_permission_id"] = self.created_from_permission_id
        return row


__all__ = [
    "EXTERNAL_MCP_CONTEXT_KIND",
    "GrantRecord",
    "KIND_DOMAIN",
    "KIND_ROOT",
    "KIND_TOOL",
    "annotations_are_read_only",
    "is_read_only",
    "resolve",
]
