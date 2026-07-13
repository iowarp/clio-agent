"""Capability / health probing through the SDK (#799 phase 1).

Encodes the capability-truth rule (SPEC §3.3): a flag advertised
``False`` — or absent — means the surface is not there; callers probe
via ``supports()`` before touching optional endpoints.
"""

from __future__ import annotations

from clio_agent.sdk import ClioClient, Health


def test_capabilities_typed_and_probeable(client: ClioClient) -> None:
    caps = client.capabilities()

    assert caps.contract_version == "0.2"
    assert caps.backend.name == "clio-agent-gact"
    assert caps.backend.vendor == "iowarp"
    assert caps.transports.events_sse is True
    assert caps.auth.current == "trust_socket"

    # Truthfully advertised surfaces (see gact test_app_scaffold).
    assert caps.supports("sessions") is True
    assert caps.supports("permissions") is True
    assert caps.supports("structured_errors") is True
    assert caps.supports("session_branching") is True

    # Capability truth: advertised False == absent == unsupported.
    assert caps.supports("voice") is False
    assert caps.supports("session_summary") is False
    assert caps.supports("this_flag_does_not_exist") is False


def test_vendor_flags_carry_rich_values(client: ClioClient) -> None:
    caps = client.capabilities()

    # x_clio_* flags are richer than booleans; supports() treats any
    # truthy value as supported, flag() exposes the raw value.
    assert caps.flag("x_clio_cancellation") == "best_effort"
    assert caps.supports("x_clio_cancellation") is True
    assert caps.flag("x_clio_synthetic_posthoc_streaming") is False


def test_supports_shorthand_caches(client: ClioClient) -> None:
    assert client.supports("sessions") is True
    # Cached: the second call reuses the parsed document object.
    assert client.capabilities() is client.capabilities()
    assert client.capabilities(refresh=True) is not None


def test_health_is_typed_with_integrations(client: ClioClient) -> None:
    health = client.health()

    assert isinstance(health, Health)
    assert isinstance(health.healthy, bool)
    assert health.overall_status in {"ready", "degraded", "unavailable"}
    assert health.integrations, "v0.2 health must include integrations[]"
    names = {row.name for row in health.integrations}
    # #800 unified /v1/health onto the runtime doctor: the rows are now the rich
    # probe set (api/arc/lm_provider/file_policy/gateway/...) instead of the old
    # hand-rolled api/sessions/agent/memory five. ``api`` is the always-present
    # row (api_state=READY is reported in-process on every call).
    assert "api" in names
    for row in health.integrations:
        assert row.status in {"ready", "degraded", "unavailable"}
