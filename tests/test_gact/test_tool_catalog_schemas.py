"""#1350: input/output schemas + declared domain on GET /v1/catalog/tools rows.

The desktop Tools view wants typed inputs/outputs and a server-declared
``domain`` to group by, without the client guessing either from the tool
NAME. These tests lock the schema/domain projection
(``gact/catalog_tool_schemas.py``) and its wire surface (the
``/v1/catalog/tools`` route).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import catalog_tool_schemas
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.gact.app import build_app
from clio_agent.gact.catalog_tool_schemas import (
    ToolSchemaError,
    builtin_tool_rows,
    tool_output_schema,
)
from clio_agent.gact.types import ToolDomain


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=None))


def _rows() -> list[Any]:
    return asyncio.run(builtin_tool_rows())


def test_every_builtin_row_has_input_schema_with_properties() -> None:
    """Every builtin row's input schema is a real JSON object schema.

    A no-argument tool (e.g. ``goal_status``) still carries an object schema
    with an empty ``properties`` -- never an omitted/empty ``input_schema``."""

    rows = _rows()
    assert len(rows) >= 25  # the ~30 built-in CLIO tools, never near-empty
    for row in rows:
        assert row.input_schema.get("type") == "object", row.name
        assert isinstance(row.input_schema.get("properties"), dict), row.name


def test_every_builtin_row_declares_domain() -> None:
    """Every builtin row carries a domain from the closed ``ToolDomain`` vocabulary."""

    from typing import get_args

    valid_domains = set(get_args(ToolDomain))
    rows = _rows()
    missing = [row.name for row in rows if not row.domain]
    assert missing == [], f"rows with no declared domain: {missing}"
    bad = [(row.name, row.domain) for row in rows if row.domain not in valid_domains]
    assert bad == [], f"rows with an out-of-vocabulary domain: {bad}"


def test_gateway_builtins_carry_gateway_description_and_schema() -> None:
    """The 4 static fs/shell rows get their schema from the gateway's own MCP listing."""

    rows = {row.name: row for row in _rows()}
    for name, expected_domain, expected_property in (
        ("fs_read_file", "workspace", "filepath"),
        ("fs_propose_edit", "workspace", "filepath"),
        ("fs_apply_edit_write", "workspace", "filepath"),
        ("shell_bash", "shell", "command"),
    ):
        row = rows[name]
        assert row.domain == expected_domain, name
        assert row.description, name
        assert expected_property in row.input_schema.get("properties", {}), name
        assert row.output_schema, name


def test_view_pdf_row_declares_domain_and_page_range_schema() -> None:
    """The declared native ``view_pdf`` row carries a workspace domain and its
    ``path``/``pages`` input schema — the catalog resolves it with
    ``supports_pdf=True`` even though no live model is bound (#1350 gate)."""

    rows = {row.name: row for row in _rows()}
    row = rows["view_pdf"]
    assert row.domain == "workspace"
    assert row.description
    properties = row.input_schema.get("properties", {})
    assert "path" in properties
    assert "pages" in properties
    assert row.output_schema


def test_output_schema_derived_from_return_annotation() -> None:
    """No per-tool special-casing: a plain ``str`` return projects to {"type": "string"}."""

    def returns_str() -> str:
        return ""

    def returns_object() -> dict[str, Any]:
        return {}

    assert tool_output_schema(returns_str, name="returns_str") == {"type": "string"}
    object_schema = tool_output_schema(returns_object, name="returns_object")
    assert object_schema.get("type") == "object"

    def returns_unannotated():  # type: ignore[no-untyped-def]
        return None

    with pytest.raises(ToolSchemaError, match="return_annotation_missing:returns_unannotated"):
        tool_output_schema(returns_unannotated, name="returns_unannotated")

    with pytest.raises(ToolSchemaError, match="return_annotation_missing:no_func"):
        tool_output_schema(None, name="no_func")


def test_output_schema_unresolvable_forward_ref_is_typed() -> None:
    """A TYPE_CHECKING-only forward-ref return annotation is a typed error, never a raw
    ``NameError`` escaping ``typing.get_type_hints`` into the route."""

    def f() -> "DefinitelyNotARealType":  # noqa: F821 - intentionally undefined, that's the point
        return None  # type: ignore[return-value]

    with pytest.raises(ToolSchemaError, match="return_annotation_unresolvable:f:NameError"):
        tool_output_schema(f, name="f")


class _OpaqueReturnType:
    """A module-level (so ``typing.get_type_hints`` can resolve the forward ref) plain class with
    no pydantic-recognizable shape -- ``Awaitable[_OpaqueReturnType]`` cannot get a json schema."""


def _returns_pydantic_unsupported_type() -> Awaitable[_OpaqueReturnType]:
    raise NotImplementedError


def test_output_schema_pydantic_unsupported_type_is_typed() -> None:
    """A return annotation pydantic cannot build a schema for (e.g. ``Awaitable[...]``, or a DSPy
    ``Prediction``) is a typed error, never a raw ``PydanticSchemaGenerationError``."""

    with pytest.raises(
        ToolSchemaError,
        match="return_annotation_unresolvable:f:PydanticSchemaGenerationError",
    ):
        tool_output_schema(_returns_pydantic_unsupported_type, name="f")


def test_builtin_tool_rows_raises_typed_error_on_missing_gateway_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A static gateway (fs/shell) row with no matching gateway listing is a typed error, never a
    silently-blank input schema/description."""

    monkeypatch.setattr(catalog_tool_schemas, "_static_gateway_rows", lambda: {})
    with pytest.raises(ToolSchemaError, match="gateway_listing_missing:"):
        asyncio.run(builtin_tool_rows())


def test_builtin_tool_rows_never_calls_the_async_gateway_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ~20ms -> ~109ms regression fix: ``GET /v1/catalog/tools`` must not spin up the fully
    async gateway client (fresh event loop + in-memory FastMCP client per namespace + psutil
    walks) on every request — it reads the memoized, synchronous, no-I/O listing instead."""

    import clio_agent.tools.gateway as gateway_module

    def _must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("list_gateway_tools must not run on the /v1/catalog/tools path")

    monkeypatch.setattr(gateway_module, "list_gateway_tools", _must_not_run)
    rows = asyncio.run(builtin_tool_rows())
    assert rows


def test_catalog_tools_route_is_fast_in_process(client: TestClient) -> None:
    """``GET /v1/catalog/tools`` serves from the memoized, synchronous static-row cache once warm
    -- no per-request event loop/FastMCP client round trip.

    The actual contract is ``test_builtin_tool_rows_never_calls_the_async_gateway_listing``
    above (a `_must_not_run` guard on the async gateway listing); a wall-clock
    ``elapsed_ms < 50`` assertion here flaked under load and was dropped (#A6 review) rather
    than fixed with a wider budget that would just flake less often.
    """

    client.get("/v1/catalog/tools")  # warm the memoized static-gateway-row cache once
    resp = client.get("/v1/catalog/tools")
    assert resp.status_code == 200


def test_native_tool_rejects_unknown_domain() -> None:
    """An unknown domain fails LOUDLY at declaration -- never coerced or defaulted."""

    def f() -> str:
        return ""

    with pytest.raises(ValueError, match="unknown domain"):
        native_tool(f, name="f", presentation="text", desc="", args={}, domain="not_a_real_domain")


def test_catalog_route_serves_schema_rows(client: TestClient) -> None:
    """GET /v1/catalog/tools rows carry input/output schemas and a domain on the wire."""

    resp = client.get("/v1/catalog/tools")
    assert resp.status_code == 200
    rows = {row["name"]: row for row in resp.json()["tools"]}

    fs_read = rows["fs_read_file"]
    assert fs_read["domain"] == "workspace"
    assert fs_read["input_schema"]["type"] == "object"
    assert "filepath" in fs_read["input_schema"]["properties"]
    assert fs_read["output_schema"]

    create_artifact = rows["create_artifact"]
    assert create_artifact["domain"] == "artifacts"
    assert create_artifact["input_schema"]["properties"]
    assert create_artifact["output_schema"]

    spawn = rows["spawn_agent_task"]
    assert spawn["domain"] == "agents"
