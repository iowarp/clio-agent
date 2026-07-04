"""Read-only catalog + config surfaces through the SDK.

Drives ``client.agents()`` / ``.tools()`` / ``.metrics()`` /
``.lm_provider()`` against the REAL in-process gact app and asserts each
decodes the live wire into the typed SDK objects the rewritten CLI
(#799 Part 3b) reads. Also asserts ``client.health().integrations``
expose the widened doctor fields (#800), and that a missing-resource
lookup maps to the shared typed SDK error the same ``_request`` helper
raises for these reads.
"""

from __future__ import annotations

import pytest

from clio_agent.sdk import (
    Agent,
    ClioClient,
    LMProvider,
    Metrics,
    NotFoundError,
    Tool,
)


def test_agents_decode_builtin_catalog(client: ClioClient) -> None:
    agents = client.agents()
    assert agents, "the built-in expert catalog must be non-empty"
    assert all(isinstance(a, Agent) for a in agents)

    by_id = {a.id: a for a in agents}
    # 'main' is the tier-1 orchestrator; always present.
    assert "main" in by_id
    main = by_id["main"]
    assert main.title  # display name the CLI /experts renders
    assert main.tier in (0, 1)

    # tier filter is honoured server-side.
    tier2 = client.agents(tier=2)
    assert all(a.tier == 2 for a in tier2)
    assert {a.id for a in tier2} <= set(by_id)


def test_tools_decode_unified_catalog(client: ClioClient) -> None:
    tools = client.tools()
    assert tools, "the unified tool catalog must expose bundled tools"
    assert all(isinstance(t, Tool) for t in tools)
    # No introspection error rows leaked into the bundled catalog.
    errors = [t for t in tools if t.source == "error"]
    assert not errors, f"tool introspection failed: {[t.description for t in errors]}"
    # Bundled fs tools are namespaced and carry a name + source.
    assert all(t.name for t in tools)
    assert any(t.source == "mcp" for t in tools)


def test_metrics_decode_runtime_counters(client: ClioClient) -> None:
    # Create a session so the sessions rollup is non-trivial.
    sess = client.sessions.create(title="metrics probe")

    metrics = client.metrics()
    assert isinstance(metrics, Metrics)
    assert metrics.uptime_s >= 0
    assert metrics.sessions.total >= 1
    assert metrics.sessions.by_status.get(sess.status, 0) >= 1
    # Nested rollups decode into their typed sub-objects.
    assert metrics.tokens.input_total >= 0
    assert metrics.cost.total_usd >= 0.0


def test_lm_provider_decodes_config(client: ClioClient) -> None:
    lm = client.lm_provider()
    assert isinstance(lm, LMProvider)
    # State is one of the wire lifecycle values.
    assert lm.state in {"idle", "configuring", "ready", "error"}
    # Presets decode into the typed preset rows (may be empty in tests,
    # but when present each carries an id/provider).
    for preset in lm.presets:
        assert preset.id
        assert preset.provider


def test_health_integrations_expose_widened_doctor_fields(client: ClioClient) -> None:
    health = client.health()
    assert health.integrations, "health must report per-subsystem integration rows"

    for row in health.integrations:
        # Back-compat triple still present.
        assert row.name
        assert row.status
        # #800 widened fields are addressable (Optional, default None) and
        # the server mirrors summary -> detail for every probed row.
        assert row.summary == row.detail
        # The attributes exist on the model regardless of value.
        assert hasattr(row, "config_source")
        assert hasattr(row, "next_action")
        assert hasattr(row, "endpoint")

    # At least one row (e.g. the LM provider) carries a config source or
    # next-action, proving the richer doctor detail survives the wire.
    assert any(r.config_source or r.next_action or r.endpoint for r in health.integrations)


def test_missing_resource_maps_to_typed_error(client: ClioClient) -> None:
    """The catalog read-methods share ``_request``'s typed error mapping;
    a missing resource surfaces as a typed :class:`NotFoundError`, not a
    raw HTTP failure."""

    with pytest.raises(NotFoundError) as excinfo:
        client.sessions.get("sess_does_not_exist")
    assert excinfo.value.status_code == 404
