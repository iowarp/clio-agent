"""Regression: federation projections seed tool definitions synchronously."""

from types import SimpleNamespace

from clio_agent.tools.gateway import list_relay_tool_definitions


def test_relay_definitions_list_from_catalog() -> None:
    catalog = SimpleNamespace(
        tools={"remote_jarvis_jarvis_run": object(), "remote_spack_spack_find": object()},
        follow_tools={"relay_read_artifact": object(), "relay_wait": object()},
    )
    federation = SimpleNamespace(catalog=catalog)

    listed = list_relay_tool_definitions(federation)

    assert set(listed) == {
        "remote_jarvis_jarvis_run",
        "remote_spack_spack_find",
        "relay_read_artifact",
        "relay_wait",
    }


def test_relay_definitions_empty_without_federation() -> None:
    assert list_relay_tool_definitions(None) == {}


def test_relay_definitions_without_follow_server_stays_catalog_only() -> None:
    """A federation stand-in with no ``follow_server`` attribute (a plain test
    double, or a pre-#1200 federation shape) must not error -- the extra seed
    is best-effort, never a hard dependency."""

    catalog = SimpleNamespace(tools={}, follow_tools={"relay_wait": object()})
    federation = SimpleNamespace(catalog=catalog)

    assert set(list_relay_tool_definitions(federation)) == {"relay_wait"}


def test_relay_definitions_include_the_locally_mounted_artifact_fetch_tool() -> None:
    """FAILING-FIRST for the v1.7.0 ``relay_fetch_artifact`` gap: this tool is
    clio-agent's own (#1200), mounted directly onto ``follow_server`` rather
    than sourced from relay's discovered catalog (relay never reports it in
    ``catalog.follow_tools``), so the catalog-only loop above never sees it.
    It must still appear in the boot preload -- exactly as if it HAD been
    catalog-reported -- because it is genuinely mounted and callable."""

    from mcp.types import Tool as McpTool

    from clio_agent.tools.relay_transport import RelayRemoteMcpCatalog
    from clio_agent.tools.remote_mcp import RemoteMcpFederation

    catalog = RelayRemoteMcpCatalog(
        revision="c" * 64,
        tools={},
        follow_tools={
            "relay_wait": McpTool(
                name="relay_wait",
                inputSchema={"type": "object", "properties": {"job_id": {"type": "string"}}},
            )
        },
    )
    federation = RemoteMcpFederation(catalog, lambda: None, cluster_hint="ares-p5run2")

    listed = list_relay_tool_definitions(federation)

    assert "relay_fetch_artifact" in listed, (
        "the locally-mounted artifact-fetch tool must seed even though relay's "
        "own catalog never reports it"
    )
    assert listed["relay_fetch_artifact"].name == "relay_fetch_artifact"
    assert "relay_wait" in listed, "the catalog-reported follow tool must still seed"


def test_relay_definitions_served_projection_wins_over_raw_catalog_entry() -> None:
    """D4 (review finding): for a name BOTH the raw catalog AND the mounted
    ``follow_server`` report, the SERVED (mounted) definition must win, not
    the raw catalog one -- ``_tool_definitions`` feeds executors'
    ``preloaded_tools`` and ``/v1/tools``, so the model must see the better
    definition. Measured drift on ``relay_observe``: catalog-reported has
    ``title=None`` and the bare relay description; served has
    ``title='Observe Job'`` and the cluster-identity sentence
    ``_with_cluster_hint`` appends. Pre-fix (``setdefault``) the raw catalog
    entry won because it was inserted first."""

    from mcp.types import Tool as McpTool

    from clio_agent.tools.relay_transport import RelayRemoteMcpCatalog
    from clio_agent.tools.remote_mcp import RemoteMcpFederation

    catalog = RelayRemoteMcpCatalog(
        revision="e" * 64,
        tools={},
        follow_tools={
            "relay_observe": McpTool(
                name="relay_observe",
                inputSchema={"type": "object", "properties": {"job_id": {"type": "string"}}},
            )
        },
    )
    federation = RemoteMcpFederation(catalog, lambda: None, cluster_hint="ares-p5run2")

    listed = list_relay_tool_definitions(federation)

    served = listed["relay_observe"]
    assert served.title == "Observe Job", "the served projection's human title must win"
    assert served.description is not None and "ares-p5run2" in served.description, (
        "the served projection's cluster-hint sentence must win"
    )
