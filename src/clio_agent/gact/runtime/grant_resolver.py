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

**Axis scoping (P0.2 #1060).** A policy row may additionally carry two OPTIONAL axis fields:
``modes`` (a ``list[str]`` of mode names) and ``on`` (a ``list[str]`` of event names). A row
whose ``modes`` is NON-EMPTY matches only when :func:`resolve`'s ``mode`` argument is one of them;
an absent/empty ``modes`` matches ANY mode (backward compatible with pre-P0.2 rows). The ``on``
axis works identically against the ``event`` argument. Two new ``kind`` discriminators ride the
same row shape: :data:`KIND_PLAN_ACL` (rows a plan-mode ACL scopes ``modes=[plan]``) and
:data:`KIND_HOOK` (rows a hook policy scopes ``on=[PreToolUse]``). P0.2 adds ONLY the engine
support for these axes/kinds; it authors NO plan/hook rules (that is P1.1/P2).
:class:`GrantRecord` round-trips both axes too (as ``tuple[str, ...]``, empty by default), so a
``plan_acl``/``hook`` grant built via ``GrantRecord(...).to_policy_row()`` keeps its ``modes``/
``on`` scoping instead of silently widening to match any mode/event on persist.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clio_agent.tools.catalog import annotations_are_read_only, get_tool_entry

#: Grant ``kind`` discriminators over the existing policy-row fields.
KIND_TOOL = "tool"
KIND_DOMAIN = "domain"
KIND_ROOT = "fs_root"
#: Axis-scoped kinds (P0.2 #1060): a plan-mode ACL row (typically ``modes=[plan]``) and a hook
#: policy row (typically ``on=[PreToolUse]``). Both ride the SAME row shape as ``kind="tool"``
#: (subject in ``tool_name_pattern``) and are matched by the shared axis step below.
KIND_PLAN_ACL = "plan_acl"
KIND_HOOK = "hook"

#: The full set of explicit ``kind`` discriminators a stored row may declare.
_VALID_KINDS = frozenset({KIND_TOOL, KIND_DOMAIN, KIND_ROOT, KIND_PLAN_ACL, KIND_HOOK})

#: The full raw action vocabulary a policy row may carry (never collapsed).
_VALID_ACTIONS = frozenset({"allow", "allow_session", "allow_workspace", "deny", "ask"})

#: Restrictiveness rank for the same-priority tie-break: higher rank WINS a tie (most
#: restrictive survives). Order (P0.1 #1059): deny > defer > ask > allow_workspace >
#: allow_session > allow. ``defer`` is absent from :data:`_VALID_ACTIONS` today (a row
#: carrying it is skipped as an invalid action) but is ranked here for forward-compat so the
#: tie-break needs no change when the vocabulary grows.
_RESTRICTIVENESS: dict[str, int] = {
    "deny": 6,
    "defer": 5,
    "ask": 4,
    "allow_workspace": 3,
    "allow_session": 2,
    "allow": 1,
}

#: The gate ``context`` kind used for an external MCP tool call. Single-sourced here so the
#: permission gate + routes import one constant and :func:`is_read_only` needs no back-import.
EXTERNAL_MCP_CONTEXT_KIND = "external_mcp"

#: Priority FLOOR for the built-in plan-mode ACL (P1.1 #1063). These are engine-level DEFAULT
#: :data:`KIND_PLAN_ACL` rows consulted by :func:`resolve` for every ``kind="tool"`` call whose
#: ``mode`` is a plan-restricted mode — they need no on-disk configuration and are NEVER persisted
#: (so a ``PUT /v1/policies`` cannot drop them and they never pollute the user's policy file). They
#: replace the mode-in-{plan,architect} read-only lock formerly copy-pasted into three modules
#: (``permission_gate``/``enrichment``/``proposal_effects``): enforcement is now ONE data-driven
#: path through :func:`resolve`. Read-only tools never reach here — :func:`is_read_only` fast-allows
#: them as the gate's structural first branch in EVERY mode, unchanged.
#:
#: These two constants are the values :func:`default_plan_acl_rows` ships and the FLOOR
#: :func:`resolve` actually enforces at runtime — NOT a fixed ceiling. :func:`_plan_acl_priorities`
#: raises the deny (and the carve-out above it) past any matching user row whose migrated legacy
#: priority (:func:`_migrated_priority`) exceeds this floor, so the mode-lock can never be
#: outranked by a large unprioritized policy store (adversarial-review finding #1, #1063).
PLAN_ACL_DENY_PRIORITY = 40
#: Priority FLOOR for the plan-mode tool allow-band (P1.4 #1066): one band above the @40 deny so the
#: non-write tools plan mode NEEDS survive the deny-everything rule. Like the deny/carve-out it is a
#: floor — :func:`_plan_acl_priorities` raises it above any matching user row so a user ``deny
#: plan_exit`` can never strand the model in plan mode.
PLAN_ACL_ALLOW_TOOL_PRIORITY = 50
PLAN_ACL_PLAN_FILE_PRIORITY = 70

#: The effectful-but-plan-safe tools plan mode ALLOWS despite the @40 deny-everything default (P1.4
#: #1066): ``plan_exit`` (the turn-ending approval yield), ``ask_user`` (clarify mid-plan), and
#: ``web_fetch`` (read external context). Reads are already fast-allowed by :func:`is_read_only`.
PLAN_ACL_PLAN_TOOLS: tuple[str, ...] = ("plan_exit", "ask_user", "web_fetch")

#: The modes the built-in plan ACL constrains. The DENY-everything default applies in both; the
#: plans-dir write carve-out and the plan-tool allow-band are ``plan`` only (architect proposes
#: diffs, it never writes files and has no plan_exit).
_PLAN_ACL_MODES = frozenset({"plan", "architect"})


def plans_dir() -> Path:
    """Return the sole writable plan-artifact directory for plan mode (P1.1 #1063).

    ``<repo>/.clio/plans`` when the current working directory is inside a VCS (``.git``) repo (so
    the plan file is committable), else ``~/.clio/plans``. Returned resolved+absolute so the
    ``path_pattern`` glob it seeds matches the resolved target path the gate hands
    :func:`resolve`. The actual plan artifact is minted in a later slice (P1.3); this helper only
    defines WHERE the single @70 write carve-out permits a ``*.md`` write.
    """

    try:
        cwd = Path.cwd()
    except OSError:
        return (Path.home() / ".clio" / "plans").resolve()
    for base in (cwd, *cwd.parents):
        # A git worktree carries a ``.git`` FILE (not a dir); ``.exists()`` covers both.
        if (base / ".git").exists():
            return (base / ".clio" / "plans").resolve()
    return (Path.home() / ".clio" / "plans").resolve()


def default_plan_acl_rows() -> list[dict[str, Any]]:
    """Return the built-in plan-mode ACL rows (P1.1 #1063 + P1.4 #1066), scoped by the ``modes`` axis.

    Priority-banded on the P0.1 model (higher wins; same-band ties most-restrictive). Each row
    carries a ``band`` tag naming its DYNAMIC priority slot (:func:`_plan_acl_priorities`); the static
    ``priority`` field is the floor that slot starts from:

    * ``deny "*" @40 modes=[plan,architect]`` — deny every non-read-only tool in plan/architect.
      Read-only tools never reach the resolver (``is_read_only`` fast-allows first), so this denies
      exactly the write/edit/shell surface, matching the deleted hardcoded lock's behaviour.
    * ``allow <plan tool> @50 modes=[plan]`` — one row per :data:`PLAN_ACL_PLAN_TOOLS` entry
      (``plan_exit``/``ask_user``/``web_fetch``): the non-write tools plan mode needs, re-allowed one
      band above the deny so the ``*`` deny does not sweep them up (P1.4 #1066).
    * ``allow "*" path=<plans>/*.md @70 modes=[plan]`` — the SOLE writable carve-out: a ``.md``
      write under the plans dir in plan mode. It beats the @40 deny by band. The ``path_pattern``
      is matched against the CALLER-NORMALIZED target path (``..`` collapsed) so a traversal such as
      ``<plans>/../src/x.py`` can never satisfy it (see :func:`_plan_acl_default_matches`).

    These are consulted directly by :func:`resolve`; they are not stored in
    ``app.state.permission_policies`` and so never migrate, flush, or affect user-row priorities.
    """

    plans = str(plans_dir())
    rows: list[dict[str, Any]] = [
        {
            "kind": KIND_PLAN_ACL,
            "action": "deny",
            "tool_name_pattern": "*",
            "modes": ["plan", "architect"],
            "priority": PLAN_ACL_DENY_PRIORITY,
            "band": "deny",
        },
    ]
    rows.extend(
        {
            "kind": KIND_PLAN_ACL,
            "action": "allow",
            "tool_name_pattern": tool_name,
            "modes": ["plan"],
            "priority": PLAN_ACL_ALLOW_TOOL_PRIORITY,
            "band": "allow_tool",
        }
        for tool_name in PLAN_ACL_PLAN_TOOLS
    )
    rows.append(
        {
            "kind": KIND_PLAN_ACL,
            "action": "allow",
            "tool_name_pattern": "*",
            "path_pattern": f"{plans}{os.sep}*.md",
            "modes": ["plan"],
            "priority": PLAN_ACL_PLAN_FILE_PRIORITY,
            "band": "plan_file",
        }
    )
    return rows


def plan_mode_deny_message(mode: str, tool_name: str = "") -> str:
    """Return the model-facing deny message for a plan-mode ACL block (P1.2 #1064).

    Replaces the generic ``"denied by permission gate"`` string the tool executor
    used to raise when a :data:`KIND_PLAN_ACL` deny wins. ``mode`` selects the
    surface-accurate wording, since ``plan`` and ``architect`` are read-only for
    DIFFERENT reasons and must not be conflated:

    * ``mode == "plan"``: names the restriction (Plan Mode is read-only), points at
      the sole writable path (the plan file), and says what IS allowed (write the
      plan, or exit plan mode to execute).
    * ``mode == "architect"`` (or any other plan-restricted mode): architect has NO
      plan-file carve-out — it proposes diffs and never writes files directly — so
      the message states that instead of pointing at a plan-file path or telling the
      model to "exit plan mode" (which is inaccurate for architect).

    This is the HUMAN/MODEL-facing text only — the typed audit reason stays
    ``policy_deny`` — and it deliberately does NOT suggest any workaround that
    defeats the mode. ``tool_name`` is woven in when known so the model sees which
    call was blocked.
    """

    tool_ref = f" ({tool_name})" if tool_name else ""
    if mode == "plan":
        plan_glob = f"{plans_dir()}{os.sep}*.md"
        return (
            f"You are in Plan Mode: read-only except the plan file at {plan_glob}. "
            f"This tool{tool_ref} would modify the system, so it is blocked. "
            "Write your plan to the plan file, or exit plan mode to execute."
        )
    return (
        f"You are in Architect Mode: propose changes as diffs; direct file "
        f"modification is blocked. This tool{tool_ref} would modify the system, so "
        "it is blocked. Describe the change as a diff for the user to apply, rather "
        "than writing or editing files directly."
    )


def _normalized_plan_acl_path(path: str) -> str:
    """Return the traversal-collapsed absolute form of ``path`` (empty on failure/absence).

    The plan-file carve-out matches ONLY this normalized form — never the raw string — so a
    ``..`` traversal is resolved away before the glob is applied (``fnmatch``'s ``*`` crosses path
    separators, which would otherwise let ``<plans>/../evil.md`` satisfy ``<plans>/*.md`` on the
    raw path). ``resolve(strict=False)`` collapses ``..`` without requiring the file to exist.
    """

    if not path:
        return ""
    try:
        return str(Path(path).resolve(strict=False))
    except OSError:
        return ""


def _plan_acl_priorities(user_matches: list[tuple[int, str]]) -> dict[str, int]:
    """Return the ``{band: priority}`` map for THIS ``resolve()`` call (P1.1 #1063 + P1.4 #1066).

    The static floor is ``(PLAN_ACL_DENY_PRIORITY, PLAN_ACL_PLAN_FILE_PRIORITY)`` = ``(40, 70)`` --
    the values :func:`default_plan_acl_rows` ships, and what a normal-sized policy store still gets.
    But a persisted store migrates unprioritized legacy USER rows to effective priority
    ``total - index`` (:func:`_migrated_priority`, P0.1 #1059): past roughly 60 legacy rows, an
    early row's migrated priority EXCEEDS the @40 floor and would otherwise outrank the plan-mode
    deny, letting a stale ``allow`` punch through plan/architect mode (P1.1 adversarial-review
    finding #1, #1063). Raising the deny to ``max(matching user priority) + 1`` -- and the
    plan-file carve-out one band above THAT -- makes the mode-lock structurally unbypassable: it
    always wins the same priority-banded comparison :func:`resolve` already runs over ``policies``,
    no matter how many legacy rows precede it or how they were migrated. ``user_matches`` is the
    ``(priority, action)`` pairs already collected from the PERSISTED policy rows for this exact
    call (same kind/subject/scope/axis filters as the built-in rows themselves), so an empty list
    (no matching user row at all) leaves the static floor untouched.
    """

    deny_priority = PLAN_ACL_DENY_PRIORITY
    if user_matches:
        deny_priority = max(deny_priority, max(priority for priority, _ in user_matches) + 1)
    # The allow-band and the plan-file carve-out both sit ABOVE the (possibly-raised) deny, so no
    # user/built-in deny can strand a plan-safe tool or the plan file; the carve-out stays highest.
    allow_tool_priority = max(PLAN_ACL_ALLOW_TOOL_PRIORITY, deny_priority + 1)
    file_priority = max(PLAN_ACL_PLAN_FILE_PRIORITY, allow_tool_priority + 1)
    return {"deny": deny_priority, "allow_tool": allow_tool_priority, "plan_file": file_priority}


def _plan_acl_default_matches(
    pattern: str, path: str, mode: str, event: str, user_matches: list[tuple[int, str]]
) -> list[tuple[int, str]]:
    """Collect ``(priority, action)`` matches from the built-in plan ACL for a tool call.

    Evaluated only for ``kind="tool"`` resolves whose ``mode`` is plan-restricted (the caller
    gates on :data:`_PLAN_ACL_MODES`). Path-bearing rows match against the NORMALIZED target path
    (:func:`_normalized_plan_acl_path`) to block traversal; tool-only rows match by the tool glob.
    The row's own static ``priority`` field is IGNORED here in favor of the dynamic per-``band``
    priority from :func:`_plan_acl_priorities` (keyed by each row's ``band`` tag) -- computed from
    ``user_matches``, the matching user rows already collected for this same call -- so the
    built-in mode-lock always outranks every user row, never just the static @40/@50/@70 floors.
    """

    priorities = _plan_acl_priorities(user_matches)
    matches: list[tuple[int, str]] = []
    for row in default_plan_acl_rows():
        if not _axis_matches(row, mode, event):
            continue
        tool_pattern = str(row.get("tool_name_pattern") or "*")
        if not fnmatch.fnmatchcase(pattern, tool_pattern):
            continue
        path_pattern = str(row.get("path_pattern") or "")
        if path_pattern:
            normalized = _normalized_plan_acl_path(path)
            if not normalized or not fnmatch.fnmatchcase(normalized, path_pattern):
                continue
        action = str(row.get("action") or "").lower()
        if action not in _VALID_ACTIONS:
            continue
        matches.append((priorities[str(row.get("band") or "deny")], action))
    return matches

#: The MCP annotation read-only classifier now lives in :mod:`clio_agent.tools.catalog` (the
#: single source of truth for tool effect classification, #1061) and is imported above +
#: re-exported here so existing importers of ``grant_resolver.annotations_are_read_only`` keep
#: working. The catalog projects the SAME predicate into each tool's read/write tag, so the
#: annotation-signal (external MCP context) and the catalog-signal below stay in lockstep.


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
    2. **Catalog signal.** The tool catalog tags the tool ``"read"`` and NOT ``"write"``. Since
       #1061 that tag is a PROJECTION of the tool's declared MCP annotations
       (:func:`clio_agent.tools.catalog.classification_tags`), spanning built-ins AND external
       MCP tools uniformly: native ``fs``/``shell`` tools carry NO gate annotation context
       (built-ins pass ``None``), so this catalog consult — fed by the fs/shell decorator
       annotations — is what classifies e.g. ``fs_read_file`` / ``fs_propose_edit``.

    Fails closed (NOT read-only → the gate proceeds to approval): ``fs_apply_edit_write`` (its
    ``destructiveHint`` projects to the ``write`` tag), ``shell_bash`` (``openWorldHint`` →
    effectful, no read tag — the OS fence, not the gate, contains its writes/egress), and any
    tool with NO annotations (fail-safe: no ``read`` tag). ``args``/``kind`` are accepted for a
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


def _axis_matches(policy: Mapping[str, Any], mode: str, event: str) -> bool:
    """Return whether ``policy``'s optional ``modes``/``on`` axes admit this call (P0.2 #1060).

    Backward-compatible narrowing filters: an ABSENT or EMPTY ``modes`` matches any ``mode`` (so a
    pre-P0.2 row is unaffected), while a NON-EMPTY ``modes`` matches ONLY when ``mode`` is one of
    its entries. The ``on`` axis narrows against ``event`` by the identical rule. Validation
    (:func:`permission_policies._validate_permission_policies`) guarantees each axis, when present,
    is a ``list[str]``; a non-list value here is treated as "no axis constraint" rather than a
    silent block, since malformed rows are rejected at the validation boundary, not here.
    """

    modes = policy.get("modes")
    if isinstance(modes, list) and modes and mode not in modes:
        return False
    on = policy.get("on")
    if isinstance(on, list) and on and event not in on:
        return False
    return True


def _migrated_priority(index: int, total: int) -> int:
    """Legacy priority for an unprioritized row: unique + DESCENDING by insertion index.

    First row (``index == 0``) gets the highest number, so a stable highest-priority-wins sort
    reproduces the historical FIRST-MATCH order EXACTLY (P0.1 #1059 migration key). Because the
    values are unique, no two legacy rows ever share a band and the most-restrictive tie-break
    can never fire for them — migrated behavior is byte-identical to the old first-match scan.
    """

    return total - index


def _effective_priority(policy: Mapping[str, Any], index: int, total: int) -> int:
    """Return ``policy``'s explicit integer priority, or its migrated legacy priority.

    An explicit ``priority`` is honoured verbatim (``bool`` is rejected — it is an ``int``
    subclass but never a valid priority). An absent/legacy priority is migrated deterministically
    via :func:`_migrated_priority` so mixed and all-legacy lists both resolve stably.
    """

    raw = policy.get("priority")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    return _migrated_priority(index, total)


def _most_restrictive(actions: list[str]) -> str:
    """Return the most-restrictive action among a same-priority band (tie-break)."""

    return max(actions, key=lambda action: _RESTRICTIVENESS.get(action, 0))


def migrate_priorities(policies: list[Any]) -> list[Any]:
    """Stamp a stable ``priority`` on every dict row lacking a valid one, in place.

    Applied on load so a persisted legacy store self-heals to explicit descending priorities
    that reproduce its historical first-match order. Rows already carrying a valid integer
    priority are left untouched. Returns ``policies`` for call-site convenience.
    """

    total = len(policies)
    for index, policy in enumerate(policies):
        if isinstance(policy, dict):
            policy["priority"] = _effective_priority(policy, index, total)
    return policies


def next_append_priority(policies: list[Any]) -> int:
    """Return a priority strictly BELOW every row's effective priority, for a new sticky append.

    Runtime sticky-grant appends (``permission_policies.py``'s ``_appended``, and
    ``routes/workspaces.py``'s ``_grant_workspace_domain``/``_grant_workspace_tool``) must land in
    their own lowest band, or an appended row left with no explicit ``priority`` collides with the
    CURRENT lowest-priority row: on an already-loaded/migrated store, an unprioritized appended row
    computes to ``total - index`` under :func:`_effective_priority`, which can equal a pre-existing
    migrated legacy row's stamped priority (both derive to ``1`` in the two-row case) -- firing the
    most-restrictive tie-break where the old first-match scan would have returned the earlier row.
    Calling :func:`migrate_priorities` AFTER appending does NOT fix this: it re-derives the same
    ``total - index`` collision.

    This computes the minimum EFFECTIVE priority across ``policies`` as they stand right now
    (migrating any still-unprioritized existing row in memory, without mutating it) and returns one
    below it -- so the new row is always strictly lower than every existing row, never ties, and
    reproduces "appended = lowest precedence = evaluated last" (today's first-match). The default
    minimum when ``policies`` holds no dict row is ``1`` (matching a fresh single-row store's
    migrated priority), so the first-ever append returns ``0``. Callers MUST call this on the list
    as it stands BEFORE appending the new row; successive calls after each append lands yield a
    strictly decreasing sequence, so N appends get N unique, monotonically lower priorities.
    """

    total = len(policies)
    minimum = 1
    found = False
    for index, policy in enumerate(policies):
        if not isinstance(policy, dict):
            continue
        priority = _effective_priority(policy, index, total)
        if not found or priority < minimum:
            minimum = priority
            found = True
    return minimum - 1


def resolve(
    kind: str,
    pattern: str,
    *,
    policies: list[Any] | None,
    session_id: str = "",
    workspace_id: str = "",
    path: str = "",
    mode: str = "",
    event: str = "",
) -> str:
    """Return the winning matching policy's RAW action, or ``""`` when nothing matches.

    One matcher over ``permission_policies`` generalizing the former ``_policy_action_for_tool``
    (``kind="tool"``) and ``_host_action_for`` (``kind="domain"``). ``pattern`` is the subject
    string for the kind: the tool name (``tool``), the requested host (``domain``), or the target
    path (``fs_root``). ``path`` supplies the optional path-glob refinement a ``tool`` policy may
    additionally carry; it is unused for ``domain``/``fs_root``. ``mode`` and ``event`` drive the
    optional ``modes``/``on`` axis narrowing (P0.2 #1060): a row with a non-empty ``modes`` matches
    only when ``mode`` is in it, and a row with a non-empty ``on`` matches only when ``event`` is in
    it; absent/empty axes match any ``mode``/``event`` (see :func:`_axis_matches`).

    **Priority-banded evaluation (P0.1 #1059).** Instead of first-match-return, ALL rows matching
    ``(kind, subject, scope)`` are collected, then the HIGHEST-priority band wins (higher priority
    number wins across bands). Within a single highest band (a TIE), the MOST-RESTRICTIVE action
    survives (``deny`` > ``defer`` > ``ask`` > ``allow_workspace`` > ``allow_session`` > ``allow``).
    Legacy rows without a ``priority`` are migrated to unique descending priorities by insertion
    index (see :func:`_migrated_priority`), so an all-legacy list reproduces the old first-match
    order exactly and the tie-break never fires for it. The raw
    ``allow``/``allow_session``/``allow_workspace``/``deny``/``ask`` action is returned verbatim.
    """

    return _winning_action(
        _collect_matches(
            kind,
            pattern,
            policies=policies,
            session_id=session_id,
            workspace_id=workspace_id,
            path=path,
            mode=mode,
            event=event,
        )
    )


def _collect_matches(
    kind: str,
    pattern: str,
    *,
    policies: list[Any] | None,
    session_id: str,
    workspace_id: str,
    path: str,
    mode: str,
    event: str,
) -> list[tuple[int, str, bool]]:
    """Collect every matching ``(priority, action, is_plan_acl)`` for a resolve call.

    Shared by :func:`resolve` and :func:`resolve_detail`. The third element flags rows
    authored by the built-in plan-mode ACL (P1.2 #1064), so :func:`resolve_detail` can
    attribute a winning deny to the mode-lock and surface its model-facing message while
    :func:`resolve` (which only needs the action) ignores it.
    """

    if not isinstance(policies, list):
        return []
    if kind == KIND_DOMAIN and (not pattern or not workspace_id):
        # _host_action_for guard: an empty host or unknown workspace can never match a host row.
        return []
    total = len(policies)
    matches: list[tuple[int, str, bool]] = []
    for index, policy in enumerate(policies):
        if not isinstance(policy, dict):
            continue
        if not _scope_matches(kind, policy, session_id, workspace_id):
            continue
        if not _subject_matches(kind, policy, pattern, path):
            continue
        if not _axis_matches(policy, mode, event):
            continue
        action = str(policy.get("action") or "").lower()
        if action not in _VALID_ACTIONS:
            continue
        matches.append((_effective_priority(policy, index, total), action, False))
    # Built-in plan-mode ACL (P1.1 #1063): the engine-level DEFAULT plan_acl rows are consulted for
    # every tool call in a plan-restricted mode, in ADDITION to the persisted user policies — never
    # merged into the user list, so they do not shift user-row insertion-index migration (the P0.1
    # golden-test key) and cannot be dropped by a ``PUT /v1/policies``. Their priority is DYNAMIC
    # (:func:`_plan_acl_priorities`), floored at @40/@70 but raised above every matching user row
    # already collected in ``matches`` -- so the mode-lock deny/carve-out always outranks a legacy
    # store's migrated priorities, however many unprioritized rows precede it (adversarial-review
    # finding #1). Gated to ``kind="tool"`` + a plan-restricted mode so a ``domain``/``fs_root``
    # resolve (or any non-plan mode) is untouched.
    if kind == KIND_TOOL and mode in _PLAN_ACL_MODES:
        user_pairs = [(priority, action) for priority, action, _src in matches]
        for priority, action in _plan_acl_default_matches(pattern, path, mode, event, user_pairs):
            matches.append((priority, action, True))
    return matches


def _winning_action(matches: list[tuple[int, str, bool]]) -> str:
    """Return the winning action for a collected match list (band + tie-break)."""

    if not matches:
        return ""
    highest = max(priority for priority, _action, _src in matches)
    band = [action for priority, action, _src in matches if priority == highest]
    if len(band) == 1:
        return band[0]
    return _most_restrictive(band)


def resolve_detail(
    kind: str,
    pattern: str,
    *,
    policies: list[Any] | None,
    session_id: str = "",
    workspace_id: str = "",
    path: str = "",
    mode: str = "",
    event: str = "",
) -> tuple[str, str]:
    """Return ``(action, deny_message)`` for a resolve call (P1.2 #1064).

    ``action`` is identical to what :func:`resolve` returns for the same arguments.
    ``deny_message`` is a non-empty, model-facing string ONLY when the winning action
    is ``deny`` AND that deny is authored by the built-in plan-mode ACL (a
    :data:`KIND_PLAN_ACL` row in the top priority band) — i.e. the write is blocked
    *because the session is in plan/architect mode*. A user-policy deny, an
    ``ask``/``allow``, or a deny in any non-plan mode returns an empty message, so the
    gate falls back to its generic denial text and only plan-mode blocks carry the
    mode-aware guidance. The typed audit reason is decided by the caller and is
    unaffected by this message.
    """

    matches = _collect_matches(
        kind,
        pattern,
        policies=policies,
        session_id=session_id,
        workspace_id=workspace_id,
        path=path,
        mode=mode,
        event=event,
    )
    action = _winning_action(matches)
    if action == "deny" and matches:
        highest = max(priority for priority, _action, _src in matches)
        plan_authored = any(
            is_plan_acl and match_action == "deny" and priority == highest
            for priority, match_action, is_plan_acl in matches
        )
        if plan_authored:
            return action, plan_mode_deny_message(mode, pattern)
    return action, ""


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
        explicit = str(row.get("kind") or "").strip()
        if explicit in _VALID_KINDS:
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
        raw_priority = row.get("priority")
        priority = raw_priority if isinstance(raw_priority, int) and not isinstance(raw_priority, bool) else None
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
        if self.priority is not None:
            row["priority"] = self.priority
        if self.modes:
            row["modes"] = list(self.modes)
        if self.on:
            row["on"] = list(self.on)
        return row


__all__ = [
    "EXTERNAL_MCP_CONTEXT_KIND",
    "GrantRecord",
    "KIND_DOMAIN",
    "KIND_HOOK",
    "KIND_PLAN_ACL",
    "KIND_ROOT",
    "KIND_TOOL",
    "PLAN_ACL_ALLOW_TOOL_PRIORITY",
    "PLAN_ACL_DENY_PRIORITY",
    "PLAN_ACL_PLAN_FILE_PRIORITY",
    "PLAN_ACL_PLAN_TOOLS",
    "annotations_are_read_only",
    "default_plan_acl_rows",
    "is_read_only",
    "migrate_priorities",
    "next_append_priority",
    "plan_mode_deny_message",
    "plans_dir",
    "resolve",
    "resolve_detail",
]
