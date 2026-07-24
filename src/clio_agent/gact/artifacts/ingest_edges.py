"""Network egress → ``used web:<domain>@<time>`` ingest provenance (B4 #978).

The clio network chokepoint (:mod:`clio_agent.runtime.net_chokepoint`) is the SOLE recorder
of child egress (the ARC-as-source invariant forbids parsing srt proxy logs). This module is
the gact-side seam it calls back into:

* :func:`install_egress_recorder` registers a closure with the chokepoint (wired from the
  server lifespan once ARC is live) that (a) appends each :class:`EgressRecord` to a bounded
  per-app ledger and (b) emits ONE trace-only ``net.egress`` semantic event via
  ``_emit_semantic_event`` — so the chokepoint never imports the god app, and every egress
  lands on the durable trace + ARC, never the SSE wire.
* :func:`attach_ingest_edges` first runs :func:`declare_web_domains` (step 0), a
  chokepoint-INDEPENDENT pass that domain-keys any used http(s)-URL edge from the tool's OWN
  declaration (``net_mechanism=tool-declared``) so a remote source survives in lineage even
  when egress recording is deferred (e.g. the codex write-fence). It then JOINS in-window
  egress records onto the ``used`` edges as ``used web:<domain>@<time>``, only ever
  STRENGTHENING a declared edge to observed evidence — **precision over recall** (#966.10):

  1. a staged-download / catalog edge whose source URL host MATCHES an in-window egress host
     is ENRICHED in place (one edge, two evidence bases: its ``sha256`` hash-pair PLUS the
     chokepoint-confirmed domain + net mechanism) — never a duplicate node;
  2. otherwise, when the transform is ingest-shaped AND the producing call's SERVING confined
     child is known AND that child's in-window egress names EXACTLY ONE (workspace-scoped)
     domain, a fresh ``web:<domain>@<time>`` edge is minted from the egress alone;
  3. an ambiguous / unattributable egress stays a BARE ``net.egress`` record — never a
     fabricated edge.

**The step-2 mint is CHILD-KEYED and deterministic** (#978 point 5: ``egress → child →
call-window → transform``). Every ``EgressRecord`` carries the ``child_id`` of the confined
child that opened the connection (a per-dispatch chokepoint channel — no timing heuristic).
The mint fires ONLY when the transform's serving child_id is threaded in AND that child's
egress is unambiguous; it ABSTAINS (bare ``net.egress``, no edge) when the serving child is
unknown, the window is unprovable (no ``started_at``), or the child's egress spans multiple
domains. This is precision over recall (#966.10): a concurrent sibling child's egress can
never be minted onto an unrelated transform. Step-1 host-match enrich is always safe (the
transform's OWN url edge corroborates the domain) and needs no child key.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlsplit

from clio_agent.gact.artifacts.transform_types import EdgeEvidence, EdgeRole, ProvEdge

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.runtime.net_chokepoint import EgressRecord

logger = logging.getLogger(__name__)

#: The trace-only semantic event a forwarded egress mints (registered in
#: ``semantic_events.SSE_TRACE_ONLY_EVENT_TYPES`` — durable-only, never on the SSE wire).
NET_EGRESS_EVENT = "net.egress"

#: Per-edge ``net_mechanism`` for a domain sourced from the TOOL's OWN url DECLARATION
#: (step 0) rather than an observed chokepoint egress — a distinct, weaker provenance source
#: than ``proxy-enforced``/``env-cooperative`` (which name an OBSERVED connection). Kept
#: separate so the trace never passes a self-report off as independent observation; a live
#: chokepoint (step 1) UPGRADES a matched edge to the observed mechanism (two evidence bases).
NET_MECHANISM_TOOL_DECLARED = "tool-declared"

#: Bound on the per-app egress ledger (bounded memory — a long-lived server must not grow it
#: unboundedly). The join only ever reads the recent window, so an old record is dead weight.
_LEDGER_MAX = 512

#: Tool (short) names whose call is INGEST-shaped even without a catalog/url used edge — a
#: generic remote fetch. Gates the egress-only mint (step 2) so a random tool that merely
#: happened to egress never grows a spurious web edge (precision over recall).
_INGEST_TOOL_HINTS: frozenset[str] = frozenset(
    {"fetch", "download", "stage_resource", "http_get", "wget", "curl", "get_url"}
)


# --------------------------------------------------------------------------- #
# The recorder: egress ledger + the trace-only ``net.egress`` emit.
# --------------------------------------------------------------------------- #


def _record_to_dict(rec: "EgressRecord") -> dict[str, Any]:
    return {
        "child_id": rec.child_id,
        "host": rec.host,
        "port": rec.port,
        "resolved_ip": rec.resolved_ip,
        "transport": rec.transport,
        "mechanism": rec.mechanism,
        "workspace_root": rec.workspace_root,
        "at": rec.at,
        # The plain-HTTP verb (N2) — the write-classification signal on the durable record
        # (``""`` for an opaque CONNECT tunnel). Additive: existing consumers ignore it.
        "method": rec.method,
    }


def record_egress(app: "FastAPI", rec: "EgressRecord") -> None:
    """Append one egress to the bounded per-app ledger AND emit a trace-only ``net.egress``.

    The recorder body the chokepoint calls back (off its accept thread). Guarded end-to-end:
    a failed ledger append or emit is a typed log that NEVER breaks egress (the chokepoint
    already caught it, but the invariant holds here too).
    """
    entry = _record_to_dict(rec)
    try:
        ledger = getattr(app.state, "net_egress_records", None)
        if not isinstance(ledger, list):
            ledger = []
            app.state.net_egress_records = ledger
        ledger.append(entry)
        del ledger[:-_LEDGER_MAX]  # bounded — oldest fall off
    except Exception:  # noqa: BLE001 — the ledger must never break egress
        logger.debug("net egress ledger append skipped reason=ledger_unwritable", exc_info=True)
    try:
        from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415

        _emit_semantic_event(
            app,
            "",  # egress is attributed by child_id, not by session (background thread)
            NET_EGRESS_EVENT,
            status="completed",
            summary=(
                f"egress {rec.transport} {rec.host}:{rec.port} "
                f"({rec.mechanism or 'unknown'}) child={rec.child_id or 'unattributed'}"
            ),
            actor={"mechanism": "harness", "component": "net-chokepoint"},
            subject={"child_id": rec.child_id, "host": rec.host, "port": rec.port},
            payload=entry,
            detail_level="off",  # durable-only (high volume); trace-only above the SSE gate
        )
    except Exception:  # noqa: BLE001 — a provenance emit must never break egress
        logger.debug("net egress event emit skipped reason=egress_emit_unavailable", exc_info=True)


def install_egress_recorder(app: "FastAPI") -> None:
    """Wire the process chokepoint's egress recorder to THIS app (server lifespan, guarded).

    Called once ARC is live (mirrors ``sandbox.emit_boot_state_event``). Registers a closure
    over ``app`` so the runtime-side chokepoint never imports gact. Idempotent — re-wiring
    simply replaces the closure with one over the same app.
    """
    try:
        from clio_agent.runtime.net_chokepoint import set_egress_recorder  # noqa: PLC0415

        set_egress_recorder(lambda rec: record_egress(app, rec))
    except Exception as exc:  # noqa: BLE001 — recorder wiring is best-effort, never blocks boot
        logger.warning(
            "net egress recorder wiring skipped reason=egress_recorder_wire_failed error=%r", exc
        )


def net_egress_records(app: "FastAPI") -> list[dict[str, Any]]:
    """Return the bounded egress ledger (empty when unset)."""
    ledger = getattr(app.state, "net_egress_records", None)
    return list(ledger) if isinstance(ledger, list) else []


# --------------------------------------------------------------------------- #
# The serving-child linkage: call_id → the confined child that served the call.
# --------------------------------------------------------------------------- #
#
# This is the deterministic key the step-2 mint requires (#978 point 5). A confined MCP
# child's ``net_child_id`` (stamped at spawn in ``sandbox.compose_confined_spawn`` →
# ``details['net_child_id']``) is associated with the ``call_id`` it serves via
# :func:`register_serving_child`; the join reads it back via
# :func:`resolve_serving_child_id`. Until a call's serving child is registered the resolver
# returns ``""`` and the step-2 mint ABSTAINS (precision over recall — never a wrong edge).
# The map is bounded and per-app; population lights up wherever the confined-dispatch boundary
# can name the child serving a call (empty on the floor, where no channel is opened at all).

#: Bound on the call_id → serving child_id map (a long-lived server must not grow it).
_SERVING_CHILD_MAX = 1024


def register_serving_child(app: "FastAPI", call_id: str, child_id: str) -> None:
    """Associate a tool ``call_id`` with the confined child that served it (B4, guarded).

    No-op for an empty ``call_id`` or ``child_id`` (the floor / unattributed case). Bounded —
    oldest associations fall off. Never raises into the caller.
    """
    call = (call_id or "").strip()
    child = (child_id or "").strip()
    if not call or not child:
        return
    try:
        table = getattr(app.state, "net_serving_child_by_call", None)
        if not isinstance(table, dict):
            table = {}
            app.state.net_serving_child_by_call = table
        table[call] = child
        if len(table) > _SERVING_CHILD_MAX:
            for stale in list(table.keys())[: len(table) - _SERVING_CHILD_MAX]:
                table.pop(stale, None)
    except Exception:  # noqa: BLE001 — a linkage note must never break a call
        logger.debug("serving-child register skipped reason=linkage_unwritable", exc_info=True)


def join_call_to_serving_child(
    app: "FastAPI", session_id: str, tool_name: str, call_id: str
) -> None:
    """Join a fleet ``call_id`` to its confined child's ``net_child_id`` (B5 #979.7, guarded).

    A namespaced tool name is ``<namespace>_<tool>``; the fleet proxy for that namespace is one
    persistent confined child whose ``net_child_id`` was registered at spawn (mcp_config
    ``transport_for`` → :func:`clio_agent.runtime.sandbox_net.register_namespace_child`), keyed
    by the workspace root. Resolve the child and record ``call_id -> net_child_id`` via
    :func:`register_serving_child` so the egress-only ingest mint (:func:`attach_ingest_edges`)
    can attribute THIS call's egress deterministically (#978 pt 5). Empty child (floor /
    built-in namespace) is a no-op — the mint abstains (precision over recall). Never raises.

    The tool-observer calls this on its ``started`` phase (the ``call_id`` mint site); the logic
    lives HERE, the serving-child seam's owner, so the observer stays a one-line call.
    """
    try:
        namespace = tool_name.split("_", 1)[0] if "_" in tool_name else tool_name
        session = app.state.sessions.get(session_id) if session_id else None
        workspace_id = str(getattr(session, "workspace_id", "") or "")
        ws = app.state.workspaces.get(workspace_id) if workspace_id else None
        workspace_root = str(getattr(ws, "root_path", "") or "")
        from clio_agent.runtime.sandbox_net import resolve_namespace_child  # noqa: PLC0415

        child = resolve_namespace_child(workspace_root, namespace)
        if child:
            register_serving_child(app, call_id, child)
    except Exception:  # noqa: BLE001 — a linkage note must never break the tool call
        logger.debug("serving-child join skipped reason=join_unavailable", exc_info=True)


def resolve_serving_child_id(app: "FastAPI", call_id: str) -> str:
    """Return the confined child id that served ``call_id``, or ``""`` (never raises).

    ``""`` is the abstain signal: the step-2 egress-only mint suppresses without a known
    serving child (precision over recall).
    """
    call = (call_id or "").strip()
    if not call:
        return ""
    table = getattr(app.state, "net_serving_child_by_call", None)
    if isinstance(table, dict):
        return str(table.get(call) or "")
    return ""


# --------------------------------------------------------------------------- #
# The join: ``used web:<domain>@<time>`` (precision over recall, #966.10).
# --------------------------------------------------------------------------- #


def _url_host(value: str) -> str:
    """Extract the host from a ``url``/``authority`` value, or ``""`` (never raises)."""
    raw = (value or "").strip()
    if not raw or raw.startswith("web:"):
        return ""
    try:
        parts = urlsplit(raw if "://" in raw else f"//{raw}")
    except ValueError:
        return ""
    return (parts.hostname or "").strip().lower()


def _epoch(iso: str) -> Optional[float]:
    """Parse an ISO-8601 timestamp to epoch seconds, or ``None`` (never raises)."""
    from datetime import datetime  # noqa: PLC0415

    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return None


def _in_window_records(
    app: "FastAPI", started_at: Optional[float], ended_at: float
) -> list[dict[str, Any]]:
    """Egress records whose timestamp falls within the transform's ``[started, ended]`` window.

    The window only BOUNDS the candidate set — the join decision itself is child-keyed +
    domain-based, not a timing heuristic. A record with an unparseable timestamp is dropped
    (never guessed into the window). When ``started_at`` is unknown the window is
    unbounded-below (still bounded above by ``ended_at``): this feeds ONLY the safe step-1
    host-match enrich (self-corroborated by the transform's own url edge). The step-2
    egress-only mint separately REQUIRES ``started_at`` (:func:`attach_ingest_edges` abstains
    without it) — an unprovable window never mints a fresh edge.
    """
    lo = started_at if started_at is not None else 0.0
    out: list[dict[str, Any]] = []
    for entry in net_egress_records(app):
        at = _epoch(str(entry.get("at") or ""))
        if at is None:
            continue
        if lo <= at <= ended_at:
            out.append(entry)
    return out


def _workspace_scoped(app: "FastAPI", records: list[dict[str, Any]], workspace_id: str) -> list:
    """Narrow records to the transform's workspace when both roots are known (best-effort).

    When the transform's workspace root is unresolvable OR a record carries no workspace_root,
    the record is KEPT (the domain-match / single-domain guards still provide precision). This
    only removes records provably belonging to a DIFFERENT workspace.
    """
    from clio_agent.gact.artifacts.minting import _workspace_root  # noqa: PLC0415

    root = _workspace_root(app, workspace_id)
    if root is None:
        return records
    root_s = str(root)
    kept: list[dict[str, Any]] = []
    for entry in records:
        ws = str(entry.get("workspace_root") or "")
        if not ws or ws == root_s or ws.startswith(root_s) or root_s.startswith(ws):
            kept.append(entry)
    return kept


def _is_ingest_shaped(tool_name: str, used: list[ProvEdge]) -> bool:
    """Whether a transform is INGEST-shaped: a known fetch tool OR a catalog/url used edge."""
    short = tool_name.rsplit(".", 1)[-1] if "." in tool_name else tool_name
    if short in _INGEST_TOOL_HINTS:
        return True
    return any(e.authority or e.external_ref.startswith("external:http") for e in used)


def _child_of(entry: dict[str, Any]) -> str:
    """The confined child id that opened this egress (``""`` when unattributed)."""
    return str(entry.get("child_id") or "").strip()


def _edge_url_host(edge: ProvEdge) -> str:
    """The http(s) host a used edge NAMES via its authority/external_ref (``""`` if none)."""
    return _url_host(edge.authority) or _url_host(edge.external_ref.removeprefix("external:"))


def _declared_web_host(value: str) -> str:
    """Host of an explicit ``http(s)`` URL, or ``""`` — REQUIRES an http/https scheme.

    Unlike :func:`_url_host` (which prepends ``//`` to a schemeless value and would mis-read a
    bare DOI / catalog id like ``10.1038/x`` or a UUID — or a non-web scheme like ``ftp://`` /
    ``s3://`` — as a "host"), the step-0 DECLARATION stamps a ``web:<domain>`` only for a genuine
    http(s) URL. Precision over recall (#966.10): never fabricate a domain from a non-web ref.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    return (parts.hostname or "").strip().lower()


def _declared_web_host_of(edge: ProvEdge) -> str:
    """The http(s) host an edge DECLARES via authority/external_ref (strict scheme; ``""`` else)."""
    return _declared_web_host(edge.authority) or _declared_web_host(
        edge.external_ref.removeprefix("external:")
    )


def declare_web_domains(used: list[ProvEdge]) -> list[ProvEdge]:
    """Step 0 — normalize TOOL-DECLARED web sources into domain-keyed edges (no chokepoint).

    General across every fetch/ingest tool (not tied to any one server): for each ``used``
    edge whose authority/external_ref names an http(s) URL and that carries no observed
    ``net_domain`` yet, stamp ``net_domain`` from the DECLARED URL host and mark
    ``net_mechanism`` :data:`NET_MECHANISM_TOOL_DECLARED`. This makes a remote source
    domain-keyed in the artifact's lineage from the tool's OWN declaration — no network
    observation required, so it holds under any write-fence whose egress recording is deferred.

    A declaration is weaker than an observed egress and is kept distinct: when the chokepoint
    is live, :func:`attach_ingest_edges` step 1 UPGRADES a matched edge to the observed
    mechanism (two evidence bases). Never mutates the input; returns a new list.
    """
    out = list(used)
    for i, edge in enumerate(out):
        if edge.net_domain or edge.net_mechanism:  # already observed/declared — never overwrite
            continue
        host = _declared_web_host_of(edge)  # STRICT: http(s) scheme only (no bare-id/DOI guess)
        if not host:
            continue
        out[i] = edge.model_copy(
            update={
                "net_domain": host,
                "net_mechanism": NET_MECHANISM_TOOL_DECLARED,
                "note": edge.note or "web_declared",
            }
        )
    return out


def attach_ingest_edges(
    app: "FastAPI",
    used: list[ProvEdge],
    *,
    workspace_id: str,
    tool_name: str,
    started_at: Optional[float],
    ended_at: Optional[float] = None,
    serving_child_id: Optional[str] = None,
) -> list[ProvEdge]:
    """Join in-window ``net.egress`` records onto a transform's ``used`` edges (B4 #978).

    Precision over recall (#966.10):

    * **Step 0 — declare** (:func:`declare_web_domains`, chokepoint-INDEPENDENT): a used edge
      naming an http(s) URL is domain-keyed from the tool's OWN declaration
      (``net_mechanism=tool-declared``) so a remote source survives in lineage even when
      egress recording is deferred. Steps 1-2 (the chokepoint) only ever strengthen it.
    * **Step 1 — upgrade** a URL edge whose host matches an in-window egress host to the
      OBSERVED mechanism (exact host match; the transform's own url edge corroborates the
      domain, so this needs no child key and is always safe — a step-0 declared edge gains the
      stronger observed evidence base here).
    * **Step 2 — egress-only mint** a fresh ``web:<domain>@<time>`` edge, but ONLY when the
      join can attribute the egress DETERMINISTICALLY to this transform's producing call
      (#978 point 5, ``egress → child → call-window → transform``): the transform is
      ingest-shaped, ``started_at`` bounds the window, ``serving_child_id`` names the confined
      child that served the call, and THAT child's in-window egress names exactly one
      still-unmatched domain.
    * **Abstain** (bare ``net.egress``, no edge) whenever step-2 attribution is not provable:
      ``serving_child_id`` is unknown/empty, the window is unbounded (no ``started_at``), or
      the serving child's egress spans multiple domains. A concurrent sibling child's egress
      is therefore never minted onto an unrelated transform.

    Returns the (possibly enriched/extended) used list; never mutates the input.
    """
    # Step 0 — tool-DECLARED web-source normalization (chokepoint-INDEPENDENT), so a remote
    # source is domain-keyed in lineage even when egress recording is deferred (e.g. codex).
    # Runs BEFORE the egress guards below; the chokepoint (steps 1-2) only ever strengthens it.
    used = declare_web_domains(used)

    window = _in_window_records(app, started_at, ended_at if ended_at is not None else time.time())
    if not window:
        return used
    records = _workspace_scoped(app, window, workspace_id)
    if not records:
        return used

    by_host: dict[str, list[dict[str, Any]]] = {}
    for entry in records:
        host = str(entry.get("host") or "").strip().lower()
        if host:
            by_host.setdefault(host, []).append(entry)

    out = list(used)
    named_hosts: set[str] = set()
    for edge in out:
        host = _edge_url_host(edge)
        if host or edge.net_domain:
            named_hosts.add(host or edge.net_domain)

    # Step 1 — UPGRADE a URL edge whose host matches an in-window egress to the OBSERVED
    # mechanism (exact host match is the guarantee: one edge, two evidence bases; never a
    # duplicate node). Safe without a child key — the transform itself named this url. A
    # step-0 ``tool-declared`` edge is upgraded here to the stronger observed evidence; an
    # already-observed edge is left as-is (idempotent, precise).
    for i, edge in enumerate(out):
        host = _edge_url_host(edge)
        if host and host in by_host and edge.net_mechanism in ("", NET_MECHANISM_TOOL_DECLARED):
            out[i] = _enriched(edge, by_host[host][0])

    # Step 2 — egress-only mint, CHILD-KEYED + abstaining (precision over recall, #978 pt 5).
    # Abstain unless the producing call's serving child is known AND the window is bounded:
    # an unprovable window or an unknown serving child can never mint a web edge.
    serving = (serving_child_id or "").strip()
    if started_at is None or not serving or not _is_ingest_shaped(tool_name, used):
        return out
    # Restrict the candidate egress to the SERVING child only — a concurrent sibling child's
    # egress (same workspace, distinct child_id) is dropped here, before the domain decision.
    child_by_host = {h: rs for h, rs in by_host.items() if any(_child_of(r) == serving for r in rs)}
    unmatched = {h: rs for h, rs in child_by_host.items() if h not in named_hosts}
    if len(unmatched) == 1:
        host, rs = next(iter(unmatched.items()))
        serving_rec = next((r for r in rs if _child_of(r) == serving), rs[0])
        out.append(_web_edge(host, serving_rec))
    return out


def _enriched(edge: ProvEdge, rec: dict[str, Any]) -> ProvEdge:
    """Enrich a URL edge in place with the chokepoint's confirmed domain + net mechanism."""
    host = str(rec.get("host") or "").strip().lower()
    return edge.model_copy(
        update={
            "net_domain": host,
            "net_mechanism": str(rec.get("mechanism") or ""),
            "net_at": str(rec.get("at") or ""),
            "net_resolved_ip": str(rec.get("resolved_ip") or ""),
            "note": edge.note or "net_ingest",
        }
    )


def _web_edge(host: str, rec: dict[str, Any]) -> ProvEdge:
    """Mint a fresh ``used web:<domain>@<time>`` edge from an egress record alone.

    An authority-evidence edge WITHOUT a content sha: a network fetch pins the input's
    IDENTITY (the domain the chokepoint observed) but not its bytes — honest for the replay
    contract (``authority``-class → re-runnable, never falsely reproducible).
    """
    at = str(rec.get("at") or "")
    return ProvEdge(
        role=EdgeRole.USED,
        evidence=EdgeEvidence.AUTHORITY,
        authority=f"web:{host}",
        external_ref=f"web:{host}@{at}",
        net_domain=host,
        net_mechanism=str(rec.get("mechanism") or ""),
        net_at=at,
        net_resolved_ip=str(rec.get("resolved_ip") or ""),
        note="net_ingest",
    )


__all__ = [
    "NET_EGRESS_EVENT",
    "NET_MECHANISM_TOOL_DECLARED",
    "attach_ingest_edges",
    "declare_web_domains",
    "install_egress_recorder",
    "join_call_to_serving_child",
    "net_egress_records",
    "record_egress",
    "register_serving_child",
    "resolve_serving_child_id",
]
