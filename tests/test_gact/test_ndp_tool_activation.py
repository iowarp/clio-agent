"""NDP schemas follow owned workspace connections, not global sign-in state."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy
import pytest

from clio_agent.gact.agents.clio_react import ClioReAct, _function_tool
from clio_agent.gact.agents.native_tool_registry import build_native_tools
from clio_agent.gact.catalog import _builtin_tool_declarations
from clio_agent.gact.storage.models import CreateSource
from clio_agent.gact.storage.ndp_catalog import NDPDataset, NDPResource
from clio_agent.gact.storage.ndp_endpoint_models import (
    EndpointConnectionInput,
    EndpointConnectionUpdate,
)
from clio_agent.gact.storage.ndp_selection import select_source
from clio_agent.gact.storage.ndp_tool_activation import session_ndp_tools_enabled
from clio_agent.gact.storage.service import StorageService
from clio_agent.gact.types import AgentDef
from tests.test_gact.test_ndp_endpoints import peer as peer_fixture
from tests.test_gact.test_source_connect_runtime import bound as bound_fixture

peer = peer_fixture
bound = bound_fixture
NDP_SETUP = {"connected_data_ndp_endpoint_connect"}
NDP_TOOLS = {
    "connected_data_search_ndp",
    "connected_data_ndp_organizations",
    "connected_data_ndp_endpoint_connect",
    "connected_data_ndp_endpoint_operations",
    "connected_data_ndp_endpoint_request",
    "connected_data_ndp_upload_artifact",
}


#: What the connected-data and NDP plugins claim for an agent that carries them.
CLAIMED = frozenset(
    NDP_TOOLS | {"connected_data_status", "connected_data_connect", "connected_data_open"}
)


def _built(claimed: frozenset[str] = CLAIMED) -> tuple[set[str], dict[str, str]]:
    """Build the claimed tools; return the model's function names and withheld reasons."""
    agent = AgentDef(id="main", title="Main", tools=sorted(claimed))
    available, _gateway, withheld = build_native_tools(agent, claimed, {})
    program = ClioReAct(dspy.Signature("question -> answer"), tools=list(available.values()))
    return {_function_tool(tool).name for tool in program.tools.values()}, withheld


def _tool_names(claimed: frozenset[str] = CLAIMED) -> set[str]:
    """Inspect the actual ReAct function declarations sent to the model."""
    return _built(claimed)[0]


def _source(service: StorageService, root: Path, workspace_id: str = "ws_default") -> Any:
    """Create a genuine approved NDP selection without fetching its bytes."""
    return select_source(
        service,
        workspace_id,
        NDPDataset(
            id="observations",
            name="observations",
            title="Observations",
            endpoint=service.ndp.endpoints()[0],
            resources=[
                NDPResource(
                    id="data", path="data.csv", name="Data", url="https://ndp.example/data.csv"
                )
            ],
        ),
        root,
    )


def test_unrelated_workspace_has_no_ndp_model_schemas_or_credential_reads(
    bound: tuple[StorageService, Any, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, upstream = bound
    service.create("ws_default", CreateSource(provider="local", root=str(upstream), label="Files"))

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Tool activation must not access accounts or the upstream catalog")

    monkeypatch.setattr(service.auth, "account_connected", forbidden)
    monkeypatch.setattr(service.auth, "token", forbidden)
    monkeypatch.setattr(service.ndp, "endpoints", forbidden)
    assert not session_ndp_tools_enabled()
    names, withheld = _built()
    assert names & NDP_TOOLS == NDP_SETUP
    assert {"connected_data_status", "connected_data_connect", "connected_data_open"} <= names
    # Claimed but unusable tools are withheld with a typed reason, never dropped silently.
    assert withheld == dict.fromkeys(NDP_TOOLS - NDP_SETUP, "ndp_source_not_connected")


def test_dataset_activation_is_recomputed_for_each_turn_of_the_same_session(
    bound: tuple[StorageService, Any, Path],
) -> None:
    service, app, _ = bound
    assert _tool_names() & NDP_TOOLS == NDP_SETUP
    record = _source(service, Path(app.state.workspaces.get("ws_default").root_path))
    assert session_ndp_tools_enabled()
    from clio_agent.gact.storage.setup_tool import connected_data_status

    selected = connected_data_status()["sources"][0]["selection"]
    assert selected == {
        "dataset_id": "observations",
        "catalog_id": service.ndp.endpoints()[0].id,
        "resources": [{"id": "data", "name": "Data", "path": "data.csv", "format": ""}],
    }
    assert "url" not in json.dumps(selected)
    assert _tool_names() & NDP_TOOLS == NDP_TOOLS
    # An agent whose plugins do not claim the NDP tools never gets them.
    assert not _tool_names(frozenset({"connected_data_status"})) & NDP_TOOLS
    service.disconnect(record)
    assert _tool_names() & NDP_TOOLS == NDP_SETUP
    # A retained snapshot or saved selection must not keep NDP enabled.
    assert service.sources("ws_default")
    record.connected = True
    service.store.put("source", record.source.id, record)
    assert _tool_names() & NDP_TOOLS == NDP_TOOLS
    record.removed = True
    service.store.put("source", record.source.id, record)
    assert _tool_names() & NDP_TOOLS == NDP_SETUP


@pytest.mark.parametrize("foreign_owner", ["workspace", "principal", "clio"])
def test_another_owner_cannot_activate_ndp(
    bound: tuple[StorageService, Any, Path], foreign_owner: str
) -> None:
    service, app, _ = bound
    record = _source(
        service,
        Path(app.state.workspaces.get("ws_default").root_path),
        "other" if foreign_owner == "workspace" else "ws_default",
    )
    if foreign_owner == "principal":
        record.principal = "someone-else"
    elif foreign_owner == "clio":
        record.source = record.source.model_copy(
            update={"owner": record.source.owner.model_copy(update={"clio_id": "other-clio"})}
        )
    service.store.put("source", record.source.id, record)
    assert _tool_names() & NDP_TOOLS == NDP_SETUP


def test_endpoint_connect_hide_restore_and_remove_change_the_model_schemas(
    bound: tuple[StorageService, Any, Path], peer: str
) -> None:
    service, _, _ = bound
    record = service.ndp_endpoints.connect(
        "ws_default", EndpointConnectionInput(name="Lab", url=peer)
    )
    assert _tool_names() & NDP_TOOLS == NDP_TOOLS
    service.ndp_endpoints.update("ws_default", record.id, EndpointConnectionUpdate(hidden=True))
    assert _tool_names() & NDP_TOOLS == NDP_SETUP
    service.ndp_endpoints.update("ws_default", record.id, EndpointConnectionUpdate(hidden=False))
    assert _tool_names() & NDP_TOOLS == NDP_TOOLS
    service.ndp_endpoints.disconnect("ws_default", record.id)
    assert _tool_names() & NDP_TOOLS == NDP_SETUP


def test_saved_catalog_connection_resolves_without_confusing_service_and_catalog_ids(
    bound: tuple[StorageService, Any, Path], peer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use a real Endpoint contract and catalog response over TCP, preserving owner scope."""
    from clio_agent.gact.storage.ndp_tools import connected_data_ndp_organizations
    from clio_agent.gact.storage.setup_tool import connected_data_status

    service, _, _ = bound
    monkeypatch.setenv(
        "CLIO_STORAGE_NDP_ENDPOINTS",
        json.dumps([{"id": "catalog", "name": "Scientific catalog", "url": peer}]),
    )
    record = service.ndp_endpoints.connect(
        "ws_default", EndpointConnectionInput(name="Saved catalog", url=peer)
    )
    assert connected_data_status()["ndp_catalogs"] == [
        {"id": "catalog", "name": "Scientific catalog", "scope": "local"}
    ]
    result = connected_data_ndp_organizations("earth", record.id)
    assert result["organizations"] == [
        {"id": "earthscope", "endpoint_name": "Scientific catalog", "endpoint_id": "catalog"}
    ]
    other = service.ndp_endpoints.connect(
        "other", EndpointConnectionInput(name="Another workspace", url=peer)
    )
    with pytest.raises(KeyError):
        connected_data_ndp_organizations("earth", other.id)
    local = service.ndp_endpoints.connect(
        "ws_default", EndpointConnectionInput(name="Different service", url=peer + "/v2")
    )
    with pytest.raises(ValueError, match="ndp_catalogs"):
        connected_data_ndp_organizations("earth", local.id)


@pytest.mark.parametrize("foreign_owner", ["workspace", "principal"])
def test_foreign_endpoint_does_not_activate_ndp(
    bound: tuple[StorageService, Any, Path], peer: str, foreign_owner: str
) -> None:
    service, _, _ = bound
    record = service.ndp_endpoints.connect(
        "other" if foreign_owner == "workspace" else "ws_default",
        EndpointConnectionInput(name="Other", url=peer),
    )
    if foreign_owner == "principal":
        record.principal = "someone-else"
        service.store.put("ndp_endpoint", record.id, record)
    assert _tool_names() & NDP_TOOLS == NDP_SETUP


def test_removing_one_of_several_sources_keeps_ndp_available(
    bound: tuple[StorageService, Any, Path], peer: str
) -> None:
    service, app, _ = bound
    source = _source(service, Path(app.state.workspaces.get("ws_default").root_path))
    endpoint = service.ndp_endpoints.connect(
        "ws_default", EndpointConnectionInput(name="Lab", url=peer)
    )
    service.disconnect(source)
    assert _tool_names() & NDP_TOOLS == NDP_TOOLS
    service.ndp_endpoints.disconnect("ws_default", endpoint.id)
    assert _tool_names() & NDP_TOOLS == NDP_SETUP


@pytest.mark.parametrize("app", [None, SimpleNamespace(state=SimpleNamespace(sessions={}))])
def test_missing_context_does_not_enable_ndp(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact.storage import ndp_tool_activation

    monkeypatch.setattr(ndp_tool_activation.context, "active_app", lambda: app)
    monkeypatch.setattr(ndp_tool_activation.context, "active_session_id", lambda: "missing")
    assert _tool_names() & NDP_TOOLS == NDP_SETUP


def test_product_catalog_still_documents_optional_ndp_tools() -> None:
    """The global tool browser describes capabilities, not a model's active schemas."""
    assert NDP_TOOLS <= {row[0] for row in _builtin_tool_declarations()}
