"""Network egress → ``used web:<domain>@<time>`` ingest provenance (B4 #978).

The clio network chokepoint (:mod:`clio_agent.runtime.net_chokepoint`) is the SOLE recorder
of child egress (the ARC-as-source invariant forbids parsing srt proxy logs). This module is
the gact-side seam it calls back into:

* :func:`install_egress_recorder` registers a closure with the chokepoint (wired from the
  server lifespan once ARC is live) that (a) appends each :class:`EgressRecord` to a bounded
  per-app ledger and (b) emits ONE trace-only ``net.egress`` semantic event via
  ``_emit_semantic_event`` — so the chokepoint never imports the god app, and every egress
  lands on the durable trace + ARC, never the SSE wire.
* :func:`attach_ingest_edges` JOINS those egress records onto an ingest-shaped
  :class:`~clio_agent.gact.artifacts.transform_types.TransformRecord`'s ``used`` edges as
  ``used web:<domain>@<time>`` — **precision over recall** (owner decision #966.10):

  1. a staged-download / catalog edge whose source URL host MATCHES an in-window egress host
     is ENRICHED in place (one edge, two evidence bases: its ``sha256`` hash-pair PLUS the
     chokepoint-confirmed domain + net mechanism) — never a duplicate node;
  2. otherwise, when the transform is ingest-shaped and the in-window egress names EXACTLY
     ONE (workspace-scoped) domain, a fresh ``web:<domain>@<time>`` edge is minted from the
     egress alone;
  3. an ambiguous (multi-domain) or unjoinable egress stays a BARE ``net.egress`` record —
     never a fabricated edge.

The join is deterministic (host match / single unambiguous domain, workspace-scoped), NOT a
timing heuristic — the window only bounds the candidate set.
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

    The window only BOUNDS the candidate set — the join decision itself is domain-based, not a
    timing heuristic. A record with an unparseable timestamp is dropped (never guessed into
    the window). When ``started_at`` is unknown the window is unbounded-below (still bounded
    above by ``ended_at``), so a just-completed ingest still joins.
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


def attach_ingest_edges(
    app: "FastAPI",
    used: list[ProvEdge],
    *,
    workspace_id: str,
    tool_name: str,
    started_at: Optional[float],
    ended_at: Optional[float] = None,
) -> list[ProvEdge]:
    """Join in-window ``net.egress`` records onto a transform's ``used`` edges (B4 #978).

    Precision over recall (#966.10): enrich a URL edge whose host matches an egress host
    (step 1); else mint ONE ``web:<domain>@<time>`` edge iff the transform is ingest-shaped
    and the in-window (workspace-scoped) egress names exactly one still-unmatched domain
    (step 2); an ambiguous / unjoinable egress stays a bare ``net.egress`` record (step 3 =
    do nothing). Returns the (possibly enriched/extended) used list; never mutates the input.
    """
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
        host = _url_host(edge.authority) or _url_host(edge.external_ref.removeprefix("external:"))
        if host or edge.net_domain:
            named_hosts.add(host or edge.net_domain)

    # Step 1 — enrich an existing URL edge whose host matches an in-window egress (exact host
    # match is the guarantee: one edge, two evidence bases; never a duplicate node).
    for i, edge in enumerate(out):
        host = _url_host(edge.authority) or _url_host(edge.external_ref.removeprefix("external:"))
        if host and host in by_host and not edge.net_domain:
            out[i] = _enriched(edge, by_host[host][0])

    # Step 2 — egress-only mint: an ingest-shaped transform whose in-window egress names
    # EXACTLY ONE still-unnamed domain gets one fresh ``web:<domain>@<time>`` edge.
    unmatched = {h: rs for h, rs in by_host.items() if h not in named_hosts}
    if len(unmatched) == 1 and _is_ingest_shaped(tool_name, used):
        host, rs = next(iter(unmatched.items()))
        out.append(_web_edge(host, rs[0]))
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
    "attach_ingest_edges",
    "install_egress_recorder",
    "net_egress_records",
    "record_egress",
]
