"""Built-in plan-mode ACL data + messaging (owner module split from ``grant_resolver``).

The plan-mode read-only lock is a cohesive sub-feature of the permission model: the DEFAULT
:data:`KIND_PLAN_ACL` rows :func:`grant_resolver.resolve` consults for every plan-restricted
``kind="tool"`` call (P1.1 #1063 + P1.4 #1066 + F1 #1057 B5), the sole writable plan-file
directory, and the mode-aware deny message the gate raises. It is factored out here (⚑ no
accretion) so :mod:`grant_resolver` stays under its size ratchet; that module imports and
re-exports every public symbol below, so existing ``grant_resolver.plans_dir`` /
``grant_resolver.default_plan_acl_rows`` / ``grant_resolver.plan_mode_deny_message`` importers are
unaffected.

This module is a LEAF (it imports nothing from :mod:`grant_resolver`), so the priority BANDS it
ships are static FLOORS only — :func:`grant_resolver._plan_acl_priorities` raises them above any
matching user row at resolve time. The ``kind`` field is written as the literal ``"plan_acl"`` (the
value of :data:`grant_resolver.KIND_PLAN_ACL`, the vocabulary owner) to keep this a leaf.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any

#: Priority FLOOR for the built-in plan-mode ACL (P1.1 #1063): engine-level DEFAULT plan_acl rows
#: :func:`grant_resolver.resolve` consults for every plan-restricted ``kind="tool"`` call. They are
#: NEVER persisted (a ``PUT /v1/policies`` cannot drop them) and replace the read-only lock formerly
#: copy-pasted into ``permission_gate``/``enrichment``/``proposal_effects``. Read-only tools never
#: reach here (:func:`grant_resolver.is_read_only` fast-allows first). This is a FLOOR, not a
#: ceiling: :func:`grant_resolver._plan_acl_priorities` raises the deny (and the carve-out above it)
#: past any matching user row's migrated priority, so a large unprioritized store can't outrank the
#: mode-lock.
PLAN_ACL_DENY_PRIORITY = 40
#: Priority FLOOR for the plan-mode tool allow-band (P1.4 #1066): one band above the @40 deny so the
#: non-write tools plan mode NEEDS survive the deny-everything rule. Like the deny/carve-out it is a
#: floor — :func:`grant_resolver._plan_acl_priorities` raises it above any matching user row so a
#: user ``deny plan_exit`` can never strand the model in plan mode. F1 (#1057 B5): this anti-lockout
#: override is NARROWED to ``plan_exit`` — for the other allow-band tools a user ``deny`` wins
#: (tighten-only, see :func:`grant_resolver._plan_acl_default_matches`); the band still RISES with
#: any user match so a frozen allow can never break a legitimately-matched user row.
PLAN_ACL_ALLOW_TOOL_PRIORITY = 50
PLAN_ACL_PLAN_FILE_PRIORITY = 70

#: The effectful-but-plan-safe tools plan mode ALLOWS despite the @40 deny-everything default (P1.4
#: #1066): ``plan_exit`` (the turn-ending approval yield), ``ask_user`` (clarify mid-plan), and
#: ``web_fetch`` (read external context). Reads are already fast-allowed by
#: :func:`grant_resolver.is_read_only`. Only ``plan_exit`` gets the unconditional anti-lockout allow
#: (F1 #1057 B5); a user ``deny`` on ``ask_user``/``web_fetch`` is honoured.
PLAN_ACL_PLAN_TOOLS: tuple[str, ...] = ("plan_exit", "ask_user", "web_fetch")

#: The modes the built-in plan ACL constrains. The DENY-everything default applies in both; the
#: plans-dir write carve-out and the plan-tool allow-band are ``plan`` only (architect proposes
#: diffs, it never writes files and has no plan_exit).
PLAN_ACL_MODES = frozenset({"plan", "architect"})


def plans_dir() -> Path:
    """Return the sole writable plan-artifact directory for plan mode (P1.1 #1063).

    ``<repo>/.clio/plans`` when the current working directory is inside a VCS (``.git``) repo (so
    the plan file is committable), else ``~/.clio/plans``. Returned resolved+absolute so the
    ``path_pattern`` glob it seeds matches the resolved target path the gate hands
    :func:`grant_resolver.resolve`. The actual plan artifact is minted in a later slice (P1.3);
    this helper only defines WHERE the single @70 write carve-out permits a ``*.md`` write.

    Returns:
        The resolved, absolute plans directory path.
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

    Priority-banded on the P0.1 model (higher wins; same-band ties most-restrictive). Each row's
    ``band`` tag names its DYNAMIC priority slot (:func:`grant_resolver._plan_acl_priorities`); the
    static ``priority`` is the floor that slot starts from:

    * ``deny "*" @40 modes=[plan,architect]`` — deny every non-read-only tool (reads fast-allow
      first, so this is exactly the write/edit/shell surface the deleted hardcoded lock covered).
    * ``allow <plan tool> @50 modes=[plan]`` — one row per :data:`PLAN_ACL_PLAN_TOOLS` entry, the
      non-write tools plan mode needs, re-allowed one band above the deny (P1.4 #1066).
    * ``allow "*" path=<plans>/*.md @70 modes=[plan]`` — the SOLE writable carve-out (a ``.md``
      write under the plans dir), matched against the CALLER-NORMALIZED target so ``..`` traversal
      can't satisfy it (see :func:`grant_resolver._plan_acl_default_matches`).

    Consulted directly by :func:`grant_resolver.resolve`; not stored in
    ``app.state.permission_policies``, so they never migrate, flush, or affect user-row priorities.

    Returns:
        A fresh list of plan-ACL policy-row dicts (``kind="plan_acl"``).
    """

    plans = str(plans_dir())
    # The literal ``"plan_acl"`` == ``grant_resolver.KIND_PLAN_ACL`` (the vocabulary owner); kept a
    # literal so this data module stays a leaf (no import cycle back into grant_resolver).
    rows: list[dict[str, Any]] = [
        {
            "kind": "plan_acl",
            "action": "deny",
            "tool_name_pattern": "*",
            "modes": ["plan", "architect"],
            "priority": PLAN_ACL_DENY_PRIORITY,
            "band": "deny",
        },
    ]
    rows.extend(
        {
            "kind": "plan_acl",
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
            "kind": "plan_acl",
            "action": "allow",
            "tool_name_pattern": "*",
            "path_pattern": f"{plans}{os.sep}*.md",
            "modes": ["plan"],
            "priority": PLAN_ACL_PLAN_FILE_PRIORITY,
            "band": "plan_file",
        }
    )
    return rows


#: Priority FLOOR for an ACTIVE operator-playbook step's narrowing deny (P1.6b #1068), used ONLY
#: outside a plan-restricted mode (execution phase). In plan mode the resolver bands the narrowing
#: at ``allow_tool + 1`` instead (see :func:`grant_resolver._plan_acl_priorities`), so it can deny a
#: plan-safe tool (``web_fetch``/``ask_user``) the @50 allow-band would otherwise grant, yet stays
#: BELOW the @70 plan-file carve-out. It is TIGHTEN-ONLY — only ``deny`` rows are ever emitted, so a
#: playbook can only remove reach, never grant it.
PLAYBOOK_STEP_DENY_PRIORITY = 41

#: Tools EXEMPT from operator-playbook narrowing (anti-lockout). ``plan_exit`` must ALWAYS remain
#: reachable — because the plan-mode narrowing band sits ABOVE the @50 plan-tool allow-band, a
#: playbook that omitted ``plan_exit`` would otherwise strand the model in plan mode. This mirrors
#: the F1 plan-ACL anti-lockout (#1057 B5), which is likewise ``plan_exit``-only: ``ask_user`` /
#: ``web_fetch`` ARE narrowable (a playbook step may legitimately forbid them).
PLAYBOOK_EXEMPT_TOOLS: tuple[str, ...] = ("plan_exit",)


def playbook_step_matches(
    pattern: str, allowed: tuple[str, ...], deny_priority: int
) -> list[tuple[int, str]]:
    """Return the narrowing ``(priority, "deny")`` for an active playbook step, or ``[]`` (P1.6b).

    When an operator playbook is active, its current step's ``tools_allowed`` is a CUGA-style
    per-step allowlist. This is the built-in consult :func:`grant_resolver.resolve` runs for a
    ``kind="tool"`` call when a step allowlist is passed (parallel to the plan-mode ACL): a tool
    whose name matches ANY allowlist glob imposes NO narrowing (``[]`` — mode + user policy decide
    it unchanged), while a tool OUTSIDE the allowlist yields a single ``deny`` so the step can only
    NARROW the surface. TIGHTEN-ONLY by construction — no ``allow`` is ever emitted, so a playbook
    can never grant a tool that mode/policy would otherwise deny.

    An EMPTY ``allowed`` (a step that declares no ``tools_allowed``) imposes no narrowing at all —
    ``tools_allowed`` is an optional per-step field. A tool in :data:`PLAYBOOK_EXEMPT_TOOLS`
    (``plan_exit``) is NEVER narrowed (anti-lockout). The caller supplies ``deny_priority`` already
    positioned for the resolve context — above the plan-tool allow-band but below the plan-file
    carve-out in plan mode, or above any matching user row otherwise — so the narrowing can neither
    be bypassed nor strand plan mode's essential paths.

    Args:
        pattern: The tool name being resolved.
        allowed: The active playbook step's ``tools_allowed`` globs (empty = no narrowing).
        deny_priority: The priority the resolver has computed for this narrowing in this context.

    Returns:
        ``[(deny_priority, "deny")]`` when the tool is outside a non-empty allowlist and not
        exempt, else ``[]``.
    """

    if not allowed:
        return []
    if pattern in PLAYBOOK_EXEMPT_TOOLS:
        return []
    if any(fnmatch.fnmatchcase(pattern, glob) for glob in allowed):
        return []
    return [(deny_priority, "deny")]


def plan_mode_deny_message(mode: str, tool_name: str = "") -> str:
    """Return the model-facing deny message for a plan-mode ACL block (P1.2 #1064).

    Replaces the generic ``"denied by permission gate"`` string the tool executor used to raise
    when a plan_acl deny wins. ``mode`` selects the surface-accurate wording, since ``plan`` and
    ``architect`` are read-only for DIFFERENT reasons and must not be conflated:

    * ``mode == "plan"``: names the restriction (Plan Mode is read-only), points at the sole
      writable path (the plan file), and says what IS allowed (write the plan, or exit plan mode
      to execute).
    * ``mode == "architect"`` (or any other plan-restricted mode): architect has NO plan-file
      carve-out — it proposes diffs and never writes files directly — so the message states that
      instead of pointing at a plan-file path or telling the model to "exit plan mode" (which is
      inaccurate for architect).

    This is the HUMAN/MODEL-facing text only — the typed audit reason stays ``policy_deny`` — and
    it deliberately does NOT suggest any workaround that defeats the mode. ``tool_name`` is woven
    in when known so the model sees which call was blocked.

    Args:
        mode: The plan-restricted mode (``plan`` or ``architect``/other).
        tool_name: The blocked tool name, woven into the message when non-empty.

    Returns:
        The mode-accurate deny message string.
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


def normalized_plan_acl_path(path: str) -> str:
    """Return the traversal-collapsed absolute form of ``path`` (empty on failure/absence).

    The plan-file carve-out matches ONLY this normalized form — never the raw string — so a ``..``
    traversal is resolved away before the glob is applied (``fnmatch``'s ``*`` crosses path
    separators, which would otherwise let ``<plans>/../evil.md`` satisfy ``<plans>/*.md`` on the raw
    path). ``resolve(strict=False)`` collapses ``..`` without requiring the file to exist.

    Args:
        path: The raw target path (may be empty).

    Returns:
        The resolved absolute path, or ``""`` when absent or unresolvable.
    """

    if not path:
        return ""
    try:
        return str(Path(path).resolve(strict=False))
    except OSError:
        return ""
