"""B4 (#978): network egress recording, per-child attribution, and the ``used web:`` join.

Host-agnostic unit coverage (the real-egress + fenced proofs run in the WSL live gate):

* ``net.egress`` record shape + emission on BOTH a CONNECT round-trip AND a plain-HTTP
  forward, carrying host/port/child_id/mechanism + the DNS-resolved ip;
* per-child attribution — a per-child channel port names that child on the egress record;
* the ``net.egress`` event is trace-only (never on the SSE wire, even on failure);
* the ``used web:<domain>@<time>`` join — enrich a staged/catalog URL edge on host match,
  mint one web edge for an unambiguous ingest egress, leave an ambiguous egress bare;
* the child-attribution wiring in ``wrap_confined`` (per-child port → the ``HTTP(S)_PROXY``
  env overlay on the Landlock/floor egress tier) and its inert floor behaviour.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from clio_agent.runtime import net_chokepoint as nc

# --------------------------------------------------------------------------- #
# net.egress record shape + emission (CONNECT + plain-HTTP), per-child attribution
# --------------------------------------------------------------------------- #


def _echo_server() -> tuple[socket.socket, int]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def _serve() -> None:
        try:
            conn, _ = srv.accept()
            with conn:
                data = conn.recv(64)
                conn.sendall(data or b"")
        except OSError:
            pass

    threading.Thread(target=_serve, daemon=True).start()
    return srv, srv.getsockname()[1]


def test_connect_round_trip_emits_egress_with_child_and_dns() -> None:
    """A CONNECT tunnel on a per-child channel records ``net.egress`` naming that child + host."""
    upstream, up_port = _echo_server()
    records: list[nc.EgressRecord] = []
    nc.set_egress_recorder(records.append)
    cp = nc.Chokepoint().start()
    try:
        port = cp.open_child_channel("child-A", mechanism="proxy-enforced", workspace_root="/ws")
        assert port > 0 and port != cp.port  # a DEDICATED per-child listener
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n\r\n".encode())
        assert b"200" in client.recv(128)
        client.sendall(b"ping")
        assert client.recv(16) == b"ping"
        client.close()
    finally:
        cp.stop()
        nc.set_egress_recorder(None)
        upstream.close()
    assert len(records) == 1
    rec = records[0]
    assert rec.child_id == "child-A"  # per-child attribution (deterministic, not timing)
    assert rec.host == "127.0.0.1" and rec.port == up_port
    assert rec.transport == "connect"
    assert rec.mechanism == "proxy-enforced"
    assert rec.resolved_ip == "127.0.0.1"  # DNS: the proxy resolved on the child's behalf
    assert rec.at  # ISO timestamp present


def test_plain_http_forward_emits_egress_with_child() -> None:
    """An absolute-form plain-HTTP forward on a per-child channel also records ``net.egress``."""
    origin = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    origin.bind(("127.0.0.1", 0))
    origin.listen(1)
    up_port = origin.getsockname()[1]

    def _serve() -> None:
        try:
            conn, _ = origin.accept()
            with conn:
                conn.recv(1024)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")
        except OSError:
            pass

    threading.Thread(target=_serve, daemon=True).start()

    records: list[nc.EgressRecord] = []
    nc.set_egress_recorder(records.append)
    cp = nc.Chokepoint().start()
    try:
        port = cp.open_child_channel("child-B", mechanism="env-cooperative")
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(
            f"GET http://127.0.0.1:{up_port}/search?q=x HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{up_port}\r\n\r\n".encode()
        )
        assert b"200 OK" in client.recv(256)
        client.close()
    finally:
        cp.stop()
        nc.set_egress_recorder(None)
        origin.close()
    assert len(records) == 1
    assert records[0].child_id == "child-B"
    assert records[0].transport == "http"
    assert records[0].mechanism == "env-cooperative"
    assert records[0].port == up_port


def test_distinct_child_channels_attribute_distinctly() -> None:
    """Two children get DISTINCT ports; each egress names the child whose listener it arrived on."""
    upstream, up_port = _echo_server()
    upstream2, up_port2 = _echo_server()
    records: list[nc.EgressRecord] = []
    nc.set_egress_recorder(records.append)
    cp = nc.Chokepoint().start()
    try:
        port_a = cp.open_child_channel("A")
        port_b = cp.open_child_channel("B")
        assert port_a != port_b
        # Idempotent: re-open A returns the same port.
        assert cp.open_child_channel("A") == port_a
        for port, up in ((port_a, up_port), (port_b, up_port2)):
            c = socket.create_connection(("127.0.0.1", port), timeout=5)
            c.sendall(f"CONNECT 127.0.0.1:{up} HTTP/1.1\r\n\r\n".encode())
            c.recv(128)
            c.close()
    finally:
        cp.stop()
        nc.set_egress_recorder(None)
        upstream.close()
        upstream2.close()
    assert {r.child_id for r in records} == {"A", "B"}


def test_close_child_channel_is_idempotent() -> None:
    cp = nc.Chokepoint().start()
    try:
        cp.open_child_channel("gone")
        cp.close_child_channel("gone")
        cp.close_child_channel("gone")  # idempotent — no raise
        # A fresh open after close yields a new listener.
        assert cp.open_child_channel("gone") > 0
    finally:
        cp.stop()


def test_recording_no_op_without_recorder() -> None:
    """No recorder wired (plain runtime/test) → egress still forwards, nothing recorded."""
    upstream, up_port = _echo_server()
    nc.set_egress_recorder(None)
    cp = nc.Chokepoint().start()
    try:
        port = cp.open_child_channel("x")
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        c.sendall(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n\r\n".encode())
        assert b"200" in c.recv(128)  # forwards fine with no recorder
        c.close()
    finally:
        cp.stop()
        upstream.close()


def test_recorder_that_raises_never_breaks_egress() -> None:
    """A raising recorder is a typed log — the tunnel still establishes (no wedge)."""
    upstream, up_port = _echo_server()

    def _boom(_rec: nc.EgressRecord) -> None:
        raise RuntimeError("recorder down")

    nc.set_egress_recorder(_boom)
    cp = nc.Chokepoint().start()
    try:
        port = cp.open_child_channel("x")
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        c.sendall(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n\r\n".encode())
        assert b"200" in c.recv(128)
        c.close()
    finally:
        cp.stop()
        nc.set_egress_recorder(None)
        upstream.close()


# --------------------------------------------------------------------------- #
# net.egress is trace-only substrate (mirror the sandbox.state / policy_violation tests)
# --------------------------------------------------------------------------- #


def test_net_egress_is_trace_only() -> None:
    from clio_agent.gact.semantic_events import SSE_TRACE_ONLY_EVENT_TYPES, event_reaches_ui

    assert "net.egress" in SSE_TRACE_ONLY_EVENT_TYPES
    assert event_reaches_ui("net.egress") is False
    assert event_reaches_ui("net.egress", status="failed") is False  # no status lifts it


def test_record_egress_appends_bounded_ledger() -> None:
    """``record_egress`` builds the durable ledger entry shape (sink absent → emit is a no-op)."""
    from fastapi import FastAPI

    from clio_agent.gact.artifacts.ingest_edges import net_egress_records, record_egress

    app = FastAPI()
    rec = nc.EgressRecord(
        child_id="c1",
        host="data.example",
        port=443,
        resolved_ip="203.0.113.9",
        transport="connect",
        mechanism="proxy-enforced",
        workspace_root="/ws",
        at="2026-07-22T00:00:00+00:00",
    )
    record_egress(app, rec)
    entry = net_egress_records(app)[0]
    assert entry["child_id"] == "c1"
    assert entry["host"] == "data.example"
    assert entry["port"] == 443
    assert entry["mechanism"] == "proxy-enforced"
    assert entry["resolved_ip"] == "203.0.113.9"
    assert entry["transport"] == "connect"


def test_real_connect_emits_net_egress_event_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """A REAL CONNECT through the Chokepoint, with ``record_egress`` wired as the recorder,
    fires exactly one ``net.egress`` semantic EVENT carrying host/port/child_id/transport.

    Closes the loop the shape tests leave open: connection → recorder → ``record_egress`` →
    ``_emit_semantic_event`` (durable-only). The event sink is stubbed so no app/ARC is needed.
    """
    from fastapi import FastAPI

    from clio_agent.gact.artifacts import ingest_edges
    from clio_agent.gact.runtime import globals as gact_globals

    events: list[dict] = []

    def _capture(app_, sid, event_type, **kw):
        events.append({"type": event_type, "sid": sid, **kw})

    monkeypatch.setattr(gact_globals, "_emit_semantic_event", _capture)

    app = FastAPI()
    upstream, up_port = _echo_server()
    nc.set_egress_recorder(lambda rec: ingest_edges.record_egress(app, rec))
    cp = nc.Chokepoint().start()
    try:
        port = cp.open_child_channel("child-Z", mechanism="proxy-enforced", workspace_root="/ws")
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        c.sendall(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n\r\n".encode())
        assert b"200" in c.recv(128)
        c.close()
    finally:
        cp.stop()
        nc.set_egress_recorder(None)
        upstream.close()

    egress_events = [e for e in events if e["type"] == "net.egress"]
    assert len(egress_events) == 1
    ev = egress_events[0]
    assert ev["detail_level"] == "off"  # durable-only, never on the SSE wire
    payload = ev["payload"]
    assert payload["child_id"] == "child-Z"
    assert payload["host"] == "127.0.0.1"
    assert payload["port"] == up_port
    assert payload["transport"] == "connect"
    assert payload["mechanism"] == "proxy-enforced"


# --------------------------------------------------------------------------- #
# used web:<domain>@<time> join — precision over recall (#966.10)
# --------------------------------------------------------------------------- #


def _app_with_egress(records: list[dict]):
    from fastapi import FastAPI

    app = FastAPI()
    app.state.net_egress_records = list(records)
    return app


def _egress(
    host: str,
    *,
    child_id: str = "c1",
    mechanism: str = "proxy-enforced",
    at: str = "2026-07-22T00:00:00+00:00",
):
    return {
        "child_id": child_id,
        "host": host,
        "port": 443,
        "resolved_ip": "203.0.113.1",
        "transport": "connect",
        "mechanism": mechanism,
        "workspace_root": "",
        "at": at,
    }


#: A ``started_at`` epoch safely BEFORE the fixed 2026-07-22 egress ``at`` (bounds the window
#: below so the child-keyed step-2 mint can fire; ``ended_at`` defaults to now).
_WINDOW_START = 1.0


def test_join_enriches_staged_download_edge_on_host_match() -> None:
    """A staged-download URL edge whose host the chokepoint saw is ENRICHED — one edge, two bases."""
    from clio_agent.gact.artifacts.ingest_edges import attach_ingest_edges
    from clio_agent.gact.artifacts.transform_types import EdgeEvidence, EdgeRole, ProvEdge

    staged = ProvEdge(
        role=EdgeRole.USED,
        evidence=EdgeEvidence.AUTHORITY,
        authority="https://data.example/dataset/x.csv",
        external_ref="external:https://data.example/dataset/x.csv",
        sha256="a" * 64,  # the staged file's content hash (hash-pair basis)
        path="/ws/x.csv",
        note="ndp_stage_resource",
    )
    app = _app_with_egress([_egress("data.example")])
    out = attach_ingest_edges(
        app, [staged], workspace_id="", tool_name="stage_resource", started_at=None
    )
    assert len(out) == 1  # JOINED, not duplicated
    edge = out[0]
    assert edge.sha256 == "a" * 64  # hash-pair basis preserved
    assert edge.net_domain == "data.example"  # + the chokepoint-confirmed domain
    assert edge.net_mechanism == "proxy-enforced"
    assert edge.net_at == "2026-07-22T00:00:00+00:00"


def test_join_mints_web_edge_for_unambiguous_ingest() -> None:
    """An ingest call whose SERVING child made a single in-window egress mints one web edge."""
    from clio_agent.gact.artifacts.ingest_edges import attach_ingest_edges

    app = _app_with_egress([_egress("ndp.example", child_id="c1", mechanism="env-cooperative")])
    out = attach_ingest_edges(
        app,
        [],
        workspace_id="",
        tool_name="fetch",
        started_at=_WINDOW_START,
        serving_child_id="c1",  # the child that served this call == the child that egressed
    )
    assert len(out) == 1
    edge = out[0]
    assert edge.net_domain == "ndp.example"
    assert edge.external_ref == "web:ndp.example@2026-07-22T00:00:00+00:00"
    assert edge.authority == "web:ndp.example"
    assert edge.net_mechanism == "env-cooperative"
    assert edge.note == "net_ingest"


def test_join_sibling_child_egress_never_minted_onto_unrelated_transform() -> None:
    """THE false-attribution regression (#978 pt 5): a SIBLING child's egress is not minted.

    Two confined children share the workspace; the sibling ``sib`` fetched ``x.example`` in
    the window, but THIS ingest-shaped transform was served by ``mine`` (which did no fetch).
    The join must NOT mint ``web:x.example`` onto this transform — the sibling's domain is
    filtered out by the serving-child key before the single-domain decision.
    """
    from clio_agent.gact.artifacts.ingest_edges import attach_ingest_edges

    app = _app_with_egress([_egress("x.example", child_id="sib")])
    out = attach_ingest_edges(
        app,
        [],
        workspace_id="",
        tool_name="fetch",
        started_at=_WINDOW_START,
        serving_child_id="mine",  # served by a DIFFERENT child than the one that egressed
    )
    assert out == []  # no fabricated edge — a wrong edge is worse than none


def test_join_abstains_when_serving_child_unknown() -> None:
    """Unknown serving child (empty) → SUPPRESS the mint even for a single in-window domain."""
    from clio_agent.gact.artifacts.ingest_edges import attach_ingest_edges

    app = _app_with_egress([_egress("a.example", child_id="c1")])
    out = attach_ingest_edges(
        app, [], workspace_id="", tool_name="fetch", started_at=_WINDOW_START, serving_child_id=""
    )
    assert out == []


def test_join_abstains_without_started_at_even_with_serving_child() -> None:
    """An unbounded window (no ``started_at``) is unprovable → no step-2 mint (item 3)."""
    from clio_agent.gact.artifacts.ingest_edges import attach_ingest_edges

    app = _app_with_egress([_egress("a.example", child_id="c1")])
    out = attach_ingest_edges(
        app, [], workspace_id="", tool_name="fetch", started_at=None, serving_child_id="c1"
    )
    assert out == []


def test_join_ambiguous_multi_domain_for_serving_child_leaves_bare() -> None:
    """The SERVING child hitting two distinct domains → AMBIGUOUS → no edge (nothing fabricated)."""
    from clio_agent.gact.artifacts.ingest_edges import attach_ingest_edges

    app = _app_with_egress(
        [_egress("a.example", child_id="c1"), _egress("b.example", child_id="c1")]
    )
    out = attach_ingest_edges(
        app, [], workspace_id="", tool_name="fetch", started_at=_WINDOW_START, serving_child_id="c1"
    )
    assert out == []  # precision over recall


def test_join_non_ingest_tool_no_web_edge() -> None:
    """A non-ingest tool with egress but no url edge does NOT mint a web edge (precision)."""
    from clio_agent.gact.artifacts.ingest_edges import attach_ingest_edges

    app = _app_with_egress([_egress("a.example", child_id="c1")])
    out = attach_ingest_edges(
        app,
        [],
        workspace_id="",
        tool_name="run_analysis",
        started_at=_WINDOW_START,
        serving_child_id="c1",
    )
    assert out == []


def test_join_no_records_is_noop() -> None:
    from clio_agent.gact.artifacts.ingest_edges import attach_ingest_edges

    app = _app_with_egress([])
    out = attach_ingest_edges(
        app, [], workspace_id="", tool_name="fetch", started_at=_WINDOW_START, serving_child_id="c1"
    )
    assert out == []


# --------------------------------------------------------------------------- #
# Step 0 — tool-DECLARED web-source normalization (chokepoint-independent).
# Holds a remote source in lineage when egress recording is deferred (e.g. codex),
# and COMPOSES with the chokepoint (an observed egress UPGRADES a declared edge).
# --------------------------------------------------------------------------- #


def _url_authority_edge(url: str, *, note: str = "fetched"):
    from clio_agent.gact.artifacts.transform_types import EdgeEvidence, EdgeRole, ProvEdge

    return ProvEdge(
        role=EdgeRole.USED,
        evidence=EdgeEvidence.AUTHORITY,
        authority=url,
        external_ref=f"external:{url}",
        note=note,
    )


def test_declare_web_domains_keys_a_url_edge_without_any_chokepoint() -> None:
    """A used URL edge is domain-keyed from the tool's OWN declaration — NO egress records.

    This is the codex case: egress recording deferred → the ledger is empty, yet the remote
    source still lands in lineage as a ``tool-declared`` domain (not falsely marked observed).
    """
    from clio_agent.gact.artifacts.ingest_edges import (
        NET_MECHANISM_TOOL_DECLARED,
        attach_ingest_edges,
    )

    edge = _url_authority_edge("https://data.example/dataset/x.csv", note="ndp_stage_resource")
    app = _app_with_egress([])  # empty ledger — the deferred-egress world
    out = attach_ingest_edges(
        app, [edge], workspace_id="", tool_name="stage_resource", started_at=None
    )
    assert len(out) == 1  # never duplicated
    got = out[0]
    assert got.net_domain == "data.example"  # domain-keyed from the DECLARED url
    assert got.net_mechanism == NET_MECHANISM_TOOL_DECLARED  # honest: declared, not observed
    assert got.authority == "https://data.example/dataset/x.csv"  # the url edge is preserved
    assert got.note == "ndp_stage_resource"  # a pre-set note is not clobbered


def test_declare_web_domains_is_general_across_tools_not_ndp_only() -> None:
    """Any fetch tool's url edge is normalized — the mechanism is tool-agnostic (not ndp-only)."""
    from clio_agent.gact.artifacts.ingest_edges import (
        NET_MECHANISM_TOOL_DECLARED,
        attach_ingest_edges,
    )

    edge = _url_authority_edge("http://arxiv.org/abs/1234.5678", note="")  # different tool/source
    app = _app_with_egress([])
    out = attach_ingest_edges(
        app, [edge], workspace_id="", tool_name="arxiv_download", started_at=None
    )
    assert len(out) == 1 and out[0].net_domain == "arxiv.org"
    assert out[0].net_mechanism == NET_MECHANISM_TOOL_DECLARED
    assert out[0].note == "web_declared"  # default note when the edge had none


def test_declare_web_domains_leaves_local_path_edges_untouched() -> None:
    """A local ``external:/path`` used edge names no http host → never domain-keyed (precision)."""
    from clio_agent.gact.artifacts.ingest_edges import attach_ingest_edges
    from clio_agent.gact.artifacts.transform_types import EdgeEvidence, EdgeRole, ProvEdge

    local = ProvEdge(
        role=EdgeRole.USED,
        evidence=EdgeEvidence.SCHEMA_ARG,
        external_ref="external:/ws/input.csv",
        note="local_input",
    )
    app = _app_with_egress([])
    out = attach_ingest_edges(app, [local], workspace_id="", tool_name="convert", started_at=None)
    assert len(out) == 1
    assert out[0].net_domain == ""  # no host → no net stamp
    assert out[0].net_mechanism == ""


def test_chokepoint_egress_upgrades_a_tool_declared_edge_to_observed() -> None:
    """The two evidence sources COMPOSE: an observed egress UPGRADES the declared edge.

    Step 0 marks the url edge ``tool-declared``; a live chokepoint that saw the SAME host then
    upgrades it in place to the stronger observed mechanism (one edge, two evidence bases) —
    never a duplicate, and the declared mark is superseded by the observation.
    """
    from clio_agent.gact.artifacts.ingest_edges import attach_ingest_edges

    edge = _url_authority_edge("https://data.example/x.csv", note="ndp_stage_resource")
    app = _app_with_egress([_egress("data.example", mechanism="proxy-enforced")])
    out = attach_ingest_edges(
        app, [edge], workspace_id="", tool_name="stage_resource", started_at=_WINDOW_START
    )
    assert len(out) == 1  # upgraded in place, not duplicated
    assert out[0].net_domain == "data.example"
    assert out[0].net_mechanism == "proxy-enforced"  # observation SUPERSEDED the declaration
    assert out[0].net_at == "2026-07-22T00:00:00+00:00"


def test_declare_web_domains_never_downgrades_an_observed_edge() -> None:
    """An edge already carrying an observed ``net_domain`` is never overwritten by step 0."""
    from clio_agent.gact.artifacts.ingest_edges import attach_ingest_edges
    from clio_agent.gact.artifacts.transform_types import EdgeEvidence, EdgeRole, ProvEdge

    observed = ProvEdge(
        role=EdgeRole.USED,
        evidence=EdgeEvidence.AUTHORITY,
        authority="https://data.example/x.csv",
        external_ref="external:https://data.example/x.csv",
        net_domain="data.example",
        net_mechanism="proxy-enforced",  # already observed
        note="net_ingest",
    )
    app = _app_with_egress([])
    out = attach_ingest_edges(
        app, [observed], workspace_id="", tool_name="stage_resource", started_at=None
    )
    assert out[0].net_mechanism == "proxy-enforced"  # untouched — no downgrade to tool-declared


def test_declare_web_domains_ignores_bare_id_and_doi_authorities() -> None:
    """A non-URL authority (bare catalog id / DOI) is NEVER declared a web domain (precision).

    Sabotage of the strict-scheme guard: a catalog-details result with an id-only resource sets
    ``authority`` to a bare DOI/UUID. Without the http(s)-scheme check, ``urlsplit`` mis-reads
    ``10.1038/x`` as host ``10.1038`` and fabricates ``net_mechanism=tool-declared``. It must not.
    """
    from clio_agent.gact.artifacts.ingest_edges import attach_ingest_edges

    for bogus in ("10.1038/s41586-020-2649-2", "550e8400-e29b-41d4-a716-446655440000"):
        edge = _url_authority_edge(bogus, note="ndp_catalog_resource")
        app = _app_with_egress([])
        out = attach_ingest_edges(app, [edge], workspace_id="", tool_name="get_details", started_at=None)
        assert out[0].net_domain == "", f"fabricated a domain for a bare id: {bogus}"
        assert out[0].net_mechanism == ""


def test_declare_web_domains_ignores_non_http_schemes() -> None:
    """A non-web scheme (``ftp://``, ``s3://``) is not a web source → never declared (precision)."""
    from clio_agent.gact.artifacts.ingest_edges import attach_ingest_edges

    for ref in ("ftp://host.example/x", "s3://bucket/key"):
        edge = _url_authority_edge(ref, note="")
        app = _app_with_egress([])
        out = attach_ingest_edges(app, [edge], workspace_id="", tool_name="fetch", started_at=None)
        assert out[0].net_domain == "", f"declared a domain for a non-web scheme: {ref}"
        assert out[0].net_mechanism == ""


def test_step0_declared_edge_coexists_with_step2_mint_for_a_distinct_domain() -> None:
    """Composition: a step-0 DECLARED url edge (host A) + a serving-child egress on host B.

    Step 0 domain-keys the declared edge (A, tool-declared); step 2 still mints a fresh
    ``web:B`` edge for the serving child's in-window egress on the DIFFERENT host B — the
    pre-stamped A in ``named_hosts`` must neither suppress B nor duplicate A.
    """
    from clio_agent.gact.artifacts.ingest_edges import (
        NET_MECHANISM_TOOL_DECLARED,
        attach_ingest_edges,
    )

    declared = _url_authority_edge("https://a.example/x.csv", note="ndp_stage_resource")
    app = _app_with_egress([_egress("b.example", child_id="c1", mechanism="proxy-enforced")])
    out = attach_ingest_edges(
        app,
        [declared],
        workspace_id="",
        tool_name="stage_resource",
        started_at=_WINDOW_START,
        serving_child_id="c1",
    )
    domains = {e.net_domain for e in out}
    assert domains == {"a.example", "b.example"}  # both present, none suppressed
    by_domain = {e.net_domain: e for e in out}
    assert by_domain["a.example"].net_mechanism == NET_MECHANISM_TOOL_DECLARED  # A stays declared
    assert by_domain["b.example"].external_ref == "web:b.example@2026-07-22T00:00:00+00:00"  # B minted
    assert len(out) == 2  # no duplicate A


def test_serving_child_linkage_register_and_resolve_roundtrip() -> None:
    """The call_id → serving child_id linkage the observer threads: register, resolve, abstain."""
    from fastapi import FastAPI

    from clio_agent.gact.artifacts.ingest_edges import (
        register_serving_child,
        resolve_serving_child_id,
    )

    app = FastAPI()
    assert resolve_serving_child_id(app, "call_1") == ""  # unknown → abstain signal
    register_serving_child(app, "call_1", "child_abc")
    assert resolve_serving_child_id(app, "call_1") == "child_abc"
    register_serving_child(app, "", "child_x")  # empty call/child → no-op, never raises
    register_serving_child(app, "call_2", "")
    assert resolve_serving_child_id(app, "call_2") == ""


# --------------------------------------------------------------------------- #
# wrap_confined child-attribution wiring (per-child port → env proxy overlay)
# --------------------------------------------------------------------------- #


def _landlock_state():
    from clio_agent.runtime import sandbox

    return sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_LANDLOCK,
        active=True,
        reason=sandbox.REASON_FENCE_ACTIVE,
        details={"net_enforcement": "env-cooperative"},
    )


def test_wrap_confined_uses_per_child_port_and_sets_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An active Landlock spawn opens a per-child channel: its port drives the env proxy overlay."""
    from clio_agent.runtime import sandbox

    captured: dict[str, object] = {}

    def _fake_open(child_id: str, *, mechanism: str = "", workspace_root: str = "") -> int:
        captured["child_id"] = child_id
        captured["mechanism"] = mechanism
        captured["workspace_root"] = workspace_root
        return 55555

    monkeypatch.setattr(nc, "open_child_channel", _fake_open)

    confined = sandbox.wrap_confined(
        "python",
        ["-c", "print(1)"],
        write_roots=[str(tmp_path)],
        profile=sandbox.PROFILE_FLEET,
        state=_landlock_state(),
    )
    # The child env overlay routes to the per-child channel (the Landlock/floor egress tier).
    assert confined.env_overlay["HTTP_PROXY"] == "http://127.0.0.1:55555"
    assert confined.env_overlay["HTTPS_PROXY"] == "http://127.0.0.1:55555"
    assert confined.env_overlay["ALL_PROXY"] == "http://127.0.0.1:55555"
    # The mechanism label is env-cooperative on the Landlock tier; the channel is workspace-scoped.
    assert captured["mechanism"] == nc.MECHANISM_ENV_COOPERATIVE
    assert captured["workspace_root"] == str(tmp_path)
    assert confined.result.details["net_child_id"] == captured["child_id"]


def test_wrap_confined_landlock_mechanism_is_env_cooperative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the Landlock tier the per-child channel carries the honest env-cooperative label."""
    from clio_agent.runtime import sandbox

    seen: dict[str, str] = {}
    monkeypatch.setattr(
        nc,
        "open_child_channel",
        lambda cid, *, mechanism="", workspace_root="": seen.update(mechanism=mechanism) or 6000,
    )
    state = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_LANDLOCK,
        active=True,
        reason=sandbox.REASON_FENCE_ACTIVE,
        details={"net_enforcement": "env-cooperative"},
    )
    sandbox.wrap_confined(
        "tool", [], write_roots=[str(Path("/ws"))], profile=sandbox.PROFILE_SHELL, state=state
    )
    assert seen["mechanism"] == nc.MECHANISM_ENV_COOPERATIVE


def test_wrap_confined_floor_opens_no_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """The floor (inactive) never opens a channel nor sets a proxy env — inert everywhere."""
    from clio_agent.runtime import sandbox

    called = {"n": 0}
    monkeypatch.setattr(
        nc, "open_child_channel", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or 1
    )
    floor = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_NONE, active=False, reason=sandbox.REASON_NOT_INSTALLED
    )
    confined = sandbox.wrap_confined(
        "python", ["-c", "x"], write_roots=[], profile=sandbox.PROFILE_FLEET, state=floor
    )
    assert called["n"] == 0
    assert confined.env_overlay == {}
    assert confined.result.details["net_child_id"] == ""


def test_wrap_confined_channel_failure_falls_back_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A channel that cannot open → fall back to the shared port, no env proxy, typed (not silent)."""
    from clio_agent.runtime import sandbox

    monkeypatch.setattr(nc, "open_child_channel", lambda *a, **k: 0)  # cannot open
    confined = sandbox.wrap_confined(
        "python",
        ["-c", "print(1)"],
        write_roots=[str(tmp_path)],
        profile=sandbox.PROFILE_FLEET,
        state=_landlock_state(),
    )
    # Falls back to the shared chokepoint; no per-child env overlay, no attribution.
    assert confined.env_overlay == {}
    assert confined.result.details["net_child_id"] == ""


def _codex_state():
    from clio_agent.runtime import sandbox

    return sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_CODEX,
        active=True,
        reason=sandbox.REASON_FENCE_ACTIVE,
        # Recipe A: egress is RECORDED through clio's upstream chokepoint → proxy-enforced label.
        details={"net_enforcement": sandbox.NET_ENFORCEMENT_PROXY, "codex_binary": "codex"},
    )


def test_wrap_confined_codex_records_egress_recipe_a(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ACTIVE codex spawn opens a per-child channel: the overlay lands on the codex PARENT.

    Recipe A — the proxy env overlay is set on the spawned process (the codex parent), and codex
    chains its managed child proxy to this per-child clio listener. Per-child attribution holds:
    one spawn = one child_id = one port.
    """
    from clio_agent.runtime import sandbox

    # Keep the codex layer write hermetic (into tmp, never the real ~/.codex).
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    captured: dict[str, object] = {}

    def _fake_open(child_id: str, *, mechanism: str = "", workspace_root: str = "") -> int:
        captured["child_id"] = child_id
        captured["mechanism"] = mechanism
        captured["workspace_root"] = workspace_root
        return 44444

    monkeypatch.setattr(nc, "open_child_channel", _fake_open)

    confined = sandbox.wrap_confined(
        "python",
        ["-c", "print(1)"],
        write_roots=[str(tmp_path)],
        profile=sandbox.PROFILE_FLEET,
        state=_codex_state(),
    )
    # A non-empty per-child id is attributed to this spawn.
    assert confined.result.details["net_child_id"] == captured["child_id"]
    assert captured["child_id"]
    # The overlay carries the Recipe-A parent proxy pointing at the per-child loopback listener.
    assert confined.env_overlay["HTTP_PROXY"] == "http://127.0.0.1:44444"
    assert confined.env_overlay["HTTPS_PROXY"] == "http://127.0.0.1:44444"
    assert confined.env_overlay["ALL_PROXY"] == "http://127.0.0.1:44444"
    # The child cannot bypass codex's managed proxy → the honest per-edge label is proxy-enforced.
    assert captured["mechanism"] == nc.MECHANISM_PROXY_ENFORCED
    assert captured["workspace_root"] == str(tmp_path)
