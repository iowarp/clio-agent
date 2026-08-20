"""Unit tests for the derived per-call MCP timeout backstop (#1230)."""

from __future__ import annotations

from clio_agent.tools.mcp_timeout_budget import component_declared_timeout_seconds


def test_no_properties_returns_none() -> None:
    assert component_declared_timeout_seconds({}) is None


def test_field_absent_returns_none() -> None:
    assert component_declared_timeout_seconds({"pipeline_id": {"type": "string"}}) is None


def test_field_present_without_default_returns_none() -> None:
    assert component_declared_timeout_seconds({"timeout_seconds": {"type": "number"}}) is None


def test_positive_default_is_returned() -> None:
    props = {"timeout_seconds": {"type": "number", "default": 600}}
    assert component_declared_timeout_seconds(props) == 600.0


def test_zero_default_is_rejected() -> None:
    props = {"timeout_seconds": {"type": "number", "default": 0}}
    assert component_declared_timeout_seconds(props) is None


def test_negative_default_is_rejected() -> None:
    props = {"timeout_seconds": {"type": "number", "default": -5}}
    assert component_declared_timeout_seconds(props) is None


def test_non_finite_default_is_rejected() -> None:
    props = {"timeout_seconds": {"type": "number", "default": float("inf")}}
    assert component_declared_timeout_seconds(props) is None


def test_bool_default_is_rejected() -> None:
    # bool is a subclass of int in Python -- must not silently coerce to 0.0/1.0s.
    props = {"timeout_seconds": {"type": "boolean", "default": True}}
    assert component_declared_timeout_seconds(props) is None


def test_non_numeric_default_is_rejected() -> None:
    props = {"timeout_seconds": {"type": "string", "default": "soon"}}
    assert component_declared_timeout_seconds(props) is None


def test_non_mapping_property_entry_is_ignored() -> None:
    assert component_declared_timeout_seconds({"timeout_seconds": "not-a-schema"}) is None


def test_wait_timeout_seconds_default_never_counts() -> None:
    """Only ``timeout_seconds`` participates -- ``wait_timeout_seconds`` only
    ever describes an ACTIVE wait_for_terminal=True call, and that call
    already takes the #1225 unbounded path before this derivation runs."""

    props = {"wait_timeout_seconds": {"type": "number", "default": 600}}
    assert component_declared_timeout_seconds(props) is None
