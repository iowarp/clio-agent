"""Permanent guardrails locking the gact/app.py decomposition (#714).

The GACT v0.2 server used to live in a single ~24k-line
``clio_agent/gact/app.py``. Issue #714 decomposed it into a package:
the API surface moved into ``gact/routes/<concern>.py`` modules
(registered via ``register_<concern>_routes(app, deps)``), the turn
engine into ``gact/turn.py``, runtime globals into
``gact/runtime/globals.py``, and so on. ``app.py`` shrank to
``build_app`` + the lifecycle plumbing + thin re-export shims that keep
``from clio_agent.gact.app import <name>`` callers green.

These invariants are easy to silently regress with an innocuous-looking
edit (re-add a route inline, re-introduce a top-level
``import clio_agent.gact.app`` cycle, let ``app.py`` re-bloat, fork the
semantic-event funnel). Each test below pins one structural property of
the decomposition and documents WHY it matters, so a future edit that
undoes the refactor fails loudly here instead of rotting the structure.
"""

from __future__ import annotations

import ast
from pathlib import Path

import clio_agent.gact.app as gact_app
import clio_agent.gact.runtime.globals as gact_globals
from clio_agent.gact.app import build_app

# --------------------------------------------------------------------------
# Shared constants — update these DELIBERATELY when the decomposition
# legitimately changes shape, never to paper over an accidental regression.
# --------------------------------------------------------------------------

# Total number of (route, method) pairs build_app() registers. Computed
# from the current surface; a mismatch means routes were added/removed
# (fine — bump this with intent) or, more dangerously, a route was wired
# inline in app.py instead of in a gact/routes/<concern>.py module.
# 141 -> 151 (#948): +3 agent-task routes (S2, moved to distinct /agent-tasks
# paths in S4 — the original same-path claim shadowed the #18 session-task GET)
# + the MCP-app/session-lifecycle routes landed by the campaign's route modules.
# Duplicate-free verified: zero (path, method) pairs register twice.
# 151 -> 157 (#968 S2): +6 artifact routes owned by routes/artifacts.py — GET
# session/workspace artifact lists, GET by-name+ref, GET by artifact_id, GET
# /bytes, POST .../pin.
# 157 -> 158 (#970 S4): +1 alias-move route POST /v1/workspaces/{wid}/artifacts/
# {name}/aliases, owned by routes/artifact_aliases.py.
# 158 -> 161 (#971 S5): +3 lineage/transform routes owned by
# routes/artifact_lineage.py — GET /v1/artifacts/{id}/lineage, GET
# /v1/sessions/{sid}/transforms, GET /v1/transforms/{activity_id}.
# 161 -> 163 (#973 S7): +2 RO-Crate export routes owned by routes/artifact_export.py
# — GET /v1/artifacts/{id}/export, GET /v1/sessions/{sid}/export/bundle. (The S7
# slice added these route modules but left the fingerprint stale; recorded now.)
# 163 -> 164 (#979 B5): +1 mid-session root/domain grant route POST
# /v1/workspaces/{wid}/grants, owned by routes/workspaces.py (grants-on-the-record).
# 164 -> 166 (#1037 Pillar 2): +2 live-handle routes owned by routes/agent_tasks.py —
# GET /v1/agent-tasks/{id}/live (read-only projection) + POST /v1/agent-tasks/{id}/steer.
# 166 -> 163 (#1057 P2.1): -3 dead /v1/hooks CRUD routes (GET/POST/DELETE) deleted
# from routes/hooks.py — a registry no dispatcher ever fired.
# 163 -> 164 (#1057 P2.7): +1 read-only GET /v1/hooks introspection route, owned by
# routes/system.py, replacing the deleted CRUD surface with a debugging endpoint.
# 164 -> 165 (#1117 P1.7): +1 POST /v1/mcp/servers/{sid}/prompts/get protocol prompt
# fetch, owned by routes/mcp.py (prompts/get client support).
# 165 -> 168 (#1127 P2.10): +3 run-projection routes owned by
# routes/agent_tasks.py: GET /v1/runs plus POST detach and dismiss actions.
# 168 -> 170 (83f18a00, #1185/#1179): +2 routes owned by routes/relay.py and
# routes/sessions.py — GET /v1/relay/status, GET /v1/sessions/{sid}/trace.
# 170 -> 172 (ad71fe87, #1192): +2 blueprint file-browsing routes owned by
# routes/blueprints.py — GET /v1/agent-blueprints/{blueprint_id}/files
# (capped flat recursive listing) and GET
# /v1/agent-blueprints/{blueprint_id}/files/read (traversal-hardened raw
# read), registered ahead of the greedy {id:path} matcher. The commit
# shipped the routes but left this fingerprint stale; recorded now.
EXPECTED_ROUTE_METHOD_PAIRS = 172

# app.py is build_app + lifecycle + re-export shims only. The ceiling is
# the current size (~2892 lines) plus ~300 lines of headroom so ordinary
# edits don't trip it, while a wholesale re-bloat (a route cluster moving
# back in) does. Lower this as app.py shrinks further; never raise it to
# absorb code that belongs in a sibling module.
APP_PY_LINE_BUDGET = 3300

_GACT_DIR = Path(gact_app.__file__).resolve().parent


def _gact_modules_excluding_app() -> list[Path]:
    """Every ``*.py`` under ``src/clio_agent/gact/`` (recursively) except
    ``app.py`` itself and ``__init__.py`` package markers."""

    return sorted(p for p in _GACT_DIR.rglob("*.py") if p.name not in {"app.py", "__init__.py"})


def test_build_app_registers_expected_route_count() -> None:
    """ROUTE-COUNT INVARIANT.

    All HTTP routes live in ``gact/routes/<concern>.py`` and are wired by
    ``register_<concern>_routes(app, deps)``. The total (route, method)
    pair count is therefore a fingerprint of the whole surface. If this
    drifts unexpectedly, either a route was added/removed (update the
    constant with intent) or — the regression we guard against — a route
    was registered inline in ``app.py`` rather than in a routes module.
    """

    from starlette.routing import Route  # noqa: PLC0415

    app = build_app()
    pairs = sum(len(r.methods or ()) for r in app.routes if isinstance(r, Route))

    assert pairs == EXPECTED_ROUTE_METHOD_PAIRS, (
        f"build_app() registered {pairs} (route, method) pairs, expected "
        f"{EXPECTED_ROUTE_METHOD_PAIRS}. If you intentionally added/removed a "
        "route, update EXPECTED_ROUTE_METHOD_PAIRS. If you did NOT touch routes, "
        "a route may have been wired inline in app.py instead of in a "
        "gact/routes/<concern>.py module — move it back."
    )


def test_no_gact_module_imports_app_at_module_top_level() -> None:
    """NO-CYCLE INVARIANT.

    ``app.py`` imports from every sibling module to assemble the app, so a
    sibling that imports ``clio_agent.gact.app`` at module-load time would
    create an import cycle and break ``build_app`` (or force fragile import
    ordering). The decomposition keeps such back-references *lazy* — inside
    function bodies, where they run only at call time — to preserve the
    ``from clio_agent.gact.app import <name>`` re-export shims without a
    cycle.

    This walks only TOP-LEVEL and CLASS-BODY ``Import``/``ImportFrom``
    nodes (function-body imports are intentionally allowed) and asserts
    none target ``clio_agent.gact.app``.
    """

    def _violations_in(node_body: list[ast.stmt], path: Path) -> list[str]:
        found: list[str] = []
        for node in node_body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "clio_agent.gact.app" or alias.name.startswith(
                        "clio_agent.gact.app."
                    ):
                        found.append(f"{path}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "clio_agent.gact.app" or module.startswith("clio_agent.gact.app."):
                    found.append(f"{path}:{node.lineno}: from {module} import ...")
            elif isinstance(node, ast.ClassDef):
                # class bodies execute at import time too — check them.
                found.extend(_violations_in(node.body, path))
        return found

    violations: list[str] = []
    for path in _gact_modules_excluding_app():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(_violations_in(tree.body, path))

    assert not violations, (
        "Module-top-level/class-body imports of clio_agent.gact.app create an "
        "import cycle. Move these inside the function bodies that need them "
        "(lazy import, '# noqa: PLC0415'):\n" + "\n".join(violations)
    )


def test_app_py_stays_within_line_budget() -> None:
    """APP.PY BUDGET INVARIANT.

    After #714, ``app.py`` is build_app + lifecycle + re-export shims. A
    line budget (current size + ~300 headroom) ensures a future change
    can't silently move a route cluster / engine back into app.py and
    re-create the monolith. If you legitimately shrink app.py further,
    lower APP_PY_LINE_BUDGET to keep the ratchet tight.
    """

    app_py = _GACT_DIR / "app.py"
    line_count = len(app_py.read_text(encoding="utf-8").splitlines())

    assert line_count <= APP_PY_LINE_BUDGET, (
        f"app.py has {line_count} lines, over the {APP_PY_LINE_BUDGET} budget. "
        "Code that belongs in a gact/routes/<concern>.py or a sibling engine "
        "module (turn.py, delegation.py, streaming.py, ...) may have crept back "
        "into app.py — extract it instead of raising the budget."
    )


def test_semantic_event_funnel_has_single_source() -> None:
    """FUNNEL SINGLE-SOURCE INVARIANT.

    ``_emit_semantic_event`` is THE funnel every semantic event flows
    through (ARC record -> trace + SSE + hooks). It is defined exactly
    once, in ``gact/runtime/globals.py``; ``app.py`` only re-exports it so
    legacy ``from clio_agent.gact.app import _emit_semantic_event`` callers
    keep working. A second definition (e.g. someone re-adds a copy in
    app.py) would fork the highway and silently drop events on whichever
    path a given caller imported. The identity check below makes that
    impossible: both names must be the SAME object.
    """

    assert gact_app._emit_semantic_event is gact_globals._emit_semantic_event, (
        "clio_agent.gact.app._emit_semantic_event must be the SAME object as "
        "clio_agent.gact.runtime.globals._emit_semantic_event (a re-export, not "
        "a second definition). A forked funnel splits the semantic-event "
        "highway and silently drops events."
    )
