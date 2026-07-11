"""Tests for the doctor MCP-launcher provisioning check."""

from __future__ import annotations

from clio_agent.runtime.mcp_launcher import probe_mcp_launchers
from clio_agent.runtime.status import IntegrationState
from clio_agent.tools.mcp_config import spec_from_declaration


def _specs(mapping: dict[str, str]) -> dict[str, object]:
    return {name: spec_from_declaration(name, value) for name, value in mapping.items()}


def test_missing_clio_kit_launcher_yields_exact_remediation():
    """A missing ``clio-kit`` launcher yields a finding with the exact remediation."""
    specs = _specs({"ndp": "clio-kit mcp-server ndp"})

    findings = probe_mcp_launchers(specs=specs, which=lambda _cmd: None)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.state == IntegrationState.UNAVAILABLE
    assert finding.name == "mcp_launcher:clio-kit"
    assert finding.required is True
    # Exact clio-kit remediation: the install command plus the PATH step.
    assert finding.next_action == (
        "uv tool install clio-kit==2.2.3, then ensure the directory reported by "
        "`uv tool dir --bin` is on PATH for the clio-agent process."
    )
    assert "uv tool install clio-kit==2.2.3" in finding.next_action


def test_present_launcher_yields_no_finding():
    """A launcher resolvable on PATH is not reported."""
    specs = _specs({"ndp": "clio-kit mcp-server ndp"})

    findings = probe_mcp_launchers(specs=specs, which=lambda _cmd: "/usr/bin/clio-kit")

    assert findings == []


def test_generic_missing_launcher_reports_actionable_finding():
    """A non-clio-kit missing launcher reports a generic actionable remediation."""
    specs = _specs({"weather": "weather-mcp serve"})

    findings = probe_mcp_launchers(specs=specs, which=lambda _cmd: None)

    assert len(findings) == 1
    assert findings[0].name == "mcp_launcher:weather-mcp"
    assert "weather-mcp" in findings[0].next_action
    assert "PATH" in findings[0].next_action


def test_http_servers_have_no_launcher_check():
    """http(s) MCP servers declare no launcher, so they never produce a finding."""
    specs = _specs({"notion": "https://mcp.notion.com/mcp"})

    findings = probe_mcp_launchers(specs=specs, which=lambda _cmd: None)

    assert findings == []


def test_duplicate_launcher_reported_once():
    """Two servers sharing one missing launcher command yield a single finding."""
    specs = _specs({"ndp": "clio-kit mcp-server ndp", "geo": "clio-kit mcp-server geo"})

    findings = probe_mcp_launchers(specs=specs, which=lambda _cmd: None)

    assert len(findings) == 1


def test_discovery_failure_is_a_structured_reason_not_a_swallow():
    """A discovery failure surfaces a structured degraded row, never a silent pass."""

    def _boom(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("blueprint discovery exploded")

    import clio_agent.runtime.mcp_launcher as module

    original = module.discover_declared_mcp_servers
    module.discover_declared_mcp_servers = _boom  # type: ignore[assignment]
    try:
        findings = probe_mcp_launchers()
    finally:
        module.discover_declared_mcp_servers = original  # type: ignore[assignment]

    assert len(findings) == 1
    assert findings[0].name == "mcp_launchers"
    assert findings[0].state == IntegrationState.DEGRADED
    assert findings[0].required is False
    assert "blueprint discovery exploded" in findings[0].summary
