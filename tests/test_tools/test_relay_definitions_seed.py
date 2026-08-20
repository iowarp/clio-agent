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
