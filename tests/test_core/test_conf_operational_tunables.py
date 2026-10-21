"""Operational tunables resolve through the config store, not hardcoded literals.

Each knob below was a bare module-level literal. It now resolves file -> env ->
in-code default through :mod:`clio_agent.conf` under a dotted key, so a
deployment can tune it without a code change. Every test varies the CONFIG
FILE layer (``tests._config_layer.set_config``) rather than ambient process env,
because the file layer is what :mod:`clio_agent.conf` resolves ABOVE the
environment (see that module's docstring).

The sibling drift tests in ``tests/test_docs/test_env_reference.py`` prove each
key is discovered by ``scripts/gen_env_reference.py`` and documented.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent import conf
from tests._config_layer import set_config
from tests.test_arc.conftest import core_arc as core_arc


@pytest.fixture(autouse=True)
def _fresh_store():
    """Reset the process-wide config store around each test."""

    conf.reload()
    yield
    conf.reload()


# --------------------------------------------------------------------------- #
# A2UI surface bounds (gact/a2ui.py)
# --------------------------------------------------------------------------- #


def _create_message(surface_id: str = "surface_1") -> dict[str, Any]:
    from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id

    return {
        "version": "v0.9.1",
        "createSurface": {"surfaceId": surface_id, "catalogId": workspace_catalog_id()},
    }


def _data_message(index: int, surface_id: str = "surface_1") -> dict[str, Any]:
    return {
        "version": "v0.9.1",
        "updateDataModel": {"surfaceId": surface_id, "path": "/counter", "value": index},
    }


def test_a2ui_message_retention_bound_is_configurable() -> None:
    """``gact.ledger_retention.a2ui_messages.max`` bounds retained surface messages."""

    from clio_agent.gact.a2ui import apply_batch, max_a2ui_messages
    from clio_agent.gact.a2ui_catalogs.registry import CatalogRegistry

    set_config("gact", {"ledger_retention": {"a2ui_messages": {"max": 4}}})
    assert max_a2ui_messages() == 4

    messages = [_create_message()]
    messages.extend(_data_message(index) for index in range(10))
    surfaces, _ = apply_batch({}, "sess_cfg", messages, catalogs=CatalogRegistry())

    surface = surfaces[("sess_cfg", "surface_1")]
    assert len(surface.messages) == 4
    assert "createSurface" in surface.messages[0]
    assert surface.eviction_reason == "a2ui_message_limit"


def test_a2ui_message_byte_bound_is_configurable() -> None:
    """``a2ui.max_message_bytes`` bounds one encoded server->client message."""

    from clio_agent.gact.a2ui import (
        A2UIValidationError,
        max_a2ui_message_bytes,
        validate_server_message,
    )
    from clio_agent.gact.a2ui_catalogs.registry import CatalogRegistry

    set_config("a2ui", {"max_message_bytes": 64})
    assert max_a2ui_message_bytes() == 64

    with pytest.raises(A2UIValidationError, match="byte limit"):
        validate_server_message(_create_message(), catalogs=CatalogRegistry())


def test_a2ui_string_bound_is_configurable() -> None:
    """``a2ui.max_string_chars`` bounds any single string inside a payload."""

    from clio_agent.gact.a2ui import (
        A2UIValidationError,
        max_a2ui_string_chars,
        validate_server_message,
    )
    from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
    from clio_agent.gact.a2ui_catalogs.registry import CatalogRegistry

    set_config("a2ui", {"max_string_chars": 8})
    assert max_a2ui_string_chars() == 8

    catalogs = CatalogRegistry()
    workspace_entry = catalogs.get(workspace_catalog_id())
    message = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [{"id": "root", "component": "Text", "text": "x" * 64}],
        },
    }
    with pytest.raises(A2UIValidationError, match="size limit"):
        validate_server_message(message, catalogs=catalogs, catalog_entry=workspace_entry)


# --------------------------------------------------------------------------- #
# Cold MCP mount readiness (gact/mcp_readiness.py)
# --------------------------------------------------------------------------- #


class _StubExecutor:
    """Minimal live-executor surface the readiness boundary drives."""

    def __init__(self) -> None:
        self.prepared: list[str] = []

    def merge_namespace_tools(self, namespace: str, tools: Any) -> None:
        return None

    def prepare_namespace(self, namespace: str) -> None:
        self.prepared.append(namespace)


def test_mcp_mount_retry_delays_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """``tools.mcp.mount_retry_delays_s`` drives the cold-mount retry ladder."""

    from clio_agent.gact.mcp_readiness import (
        mcp_mount_retry_delays_s,
        mount_namespace_for_session,
    )
    from clio_agent.tools import mcp_discovery

    set_config("tools", {"mcp": {"mount_retry_delays_s": "0.001,0.001,0.001"}})
    assert mcp_mount_retry_delays_s() == (0.001, 0.001, 0.001)

    attempts = 0

    def _always_fail(namespace: str, spec: Any) -> Any:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("cold mount refused")

    monkeypatch.setattr(mcp_discovery, "ensure_namespace", _always_fail)
    with pytest.raises(RuntimeError, match="cold mount refused"):
        mount_namespace_for_session(_StubExecutor(), "ns", object())
    # One attempt more than there are delays.
    assert attempts == 4


def test_mcp_no_progress_window_is_configurable() -> None:
    """``tools.mcp.no_progress_s`` (replacing the fixed ``tools.mcp.setup_timeout_s``
    ladder) is the window an MCP connect may go without an answer and without work."""

    from clio_agent.tools.mcp_server_progress import mcp_no_progress_s

    assert mcp_no_progress_s() == 30.0
    set_config("tools", {"mcp": {"no_progress_s": 45}})
    assert mcp_no_progress_s() == 45.0


# --------------------------------------------------------------------------- #
# Artifact table preview (gact/routes/artifact_table_preview.py)
# --------------------------------------------------------------------------- #


def test_table_preview_row_ceiling_is_configurable() -> None:
    """``artifacts.table_preview_max_rows`` clamps the requested row count."""

    from clio_agent.gact.routes.artifact_table_preview import (
        _bounded_preview_limit,
        table_preview_max_rows,
    )

    set_config("artifacts", {"table_preview_max_rows": 25})
    assert table_preview_max_rows() == 25
    assert _bounded_preview_limit(10_000) == 25
    assert _bounded_preview_limit(0) == 1


def test_table_preview_source_byte_ceiling_is_configurable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``artifacts.table_preview_max_source_bytes`` refuses an oversized CSV."""

    from fastapi import HTTPException

    from clio_agent.gact.routes import artifact_table_preview as module

    set_config("artifacts", {"table_preview_max_source_bytes": 16})
    assert module.table_preview_max_source_bytes() == 16

    source = tmp_path / "big.csv"
    source.write_text("a,b\n" + "1,2\n" * 40, encoding="utf-8")
    monkeypatch.setattr(module, "_artifact_source", lambda app, record, version: source)

    record = SimpleNamespace(name="big.csv", workspace_id="ws")
    version = SimpleNamespace(artifact_id="art_1")
    with pytest.raises(HTTPException) as excinfo:
        module._csv_preview(SimpleNamespace(), record, version, ["a", "b"], 10)
    assert excinfo.value.status_code == 413
    assert excinfo.value.detail["error"]["details"]["max_bytes"] == 16


# --------------------------------------------------------------------------- #
# Artifact table query (gact/routes/artifact_table_query.py)
# --------------------------------------------------------------------------- #


def test_table_query_knobs_are_configurable() -> None:
    """``artifacts.table_query_*`` resolve through the config file layer."""

    from clio_agent.gact.routes import artifact_table_query as module

    assert module.table_query_max_rows() == 50_000
    assert module.table_query_max_source_bytes() == 256 * 1024 * 1024
    assert module.table_query_no_progress_s() == 30.0
    assert module.table_query_max_wait_s() == 180.0
    assert module.table_query_cache_entries() == 16

    set_config(
        "artifacts",
        {
            "table_query_max_rows": 40,
            "table_query_max_source_bytes": 1024,
            "table_query_no_progress_s": 2.5,
            "table_query_max_wait_s": 60.0,
            "table_query_cache_entries": 0,
        },
    )
    assert module.table_query_max_rows() == 40
    assert module.table_query_max_source_bytes() == 1024
    assert module.table_query_no_progress_s() == 2.5
    assert module.table_query_max_wait_s() == 60.0
    assert module.table_query_cache_entries() == 0
    assert module._effective_limit(None) == 40


# --------------------------------------------------------------------------- #
# NOTE (codex migration): the deleted Codex SDK provider spawned per-session
# CODEX_HOME dirs (isolated credential homes) with a configurable capacity
# (``providers.codex.credential_home_capacity``,
# ``providers/codex_credential_home.py``). The direct Codex provider owns
# ONE durable credential file end to end
# (``clio_agent.providers.codex.credentials.CodexCredentialStore``, see
# ``gact/app.py``'s note next to the deleted reaper) -- there is no spawned
# home, and so no equivalent capacity knob to test here.
# --------------------------------------------------------------------------- #


def test_spotter_clearance_event_retention_is_configurable() -> None:
    """``spotter.max_clearance_events`` triggers the watcher-less prune."""

    from clio_agent.gact.spotter_clearance import clearance_event, max_clearance_events

    set_config("spotter", {"max_clearance_events": 2})
    assert max_clearance_events() == 2

    app = SimpleNamespace(state=SimpleNamespace())
    clearance_event(app, "sess_a")
    clearance_event(app, "sess_b")
    assert len(app.state.spotter_clearance_events) == 2
    # The third creation reaches the bound: no session has a live watcher, so
    # every existing entry is released and only the new one remains.
    clearance_event(app, "sess_c")
    assert list(app.state.spotter_clearance_events) == ["sess_c"]


# --------------------------------------------------------------------------- #
# Cooperative-cancel grace (gact/routes/session_cancellation.py)
# --------------------------------------------------------------------------- #


def test_conversation_stop_has_no_automatic_retirement_deadline() -> None:
    """Stop follows physical worker retirement rather than a configured grace cutoff."""
    import inspect

    from clio_agent.gact.turn_runner import TurnRunner

    assert set(inspect.signature(TurnRunner.stop).parameters) == {"self", "sid", "reservation"}
    assert set(inspect.signature(TurnRunner.drain).parameters) == {"self"}


# --------------------------------------------------------------------------- #
# Model-facing MCP result bound (tools/mcp_result_projection.py)
# --------------------------------------------------------------------------- #


def test_model_tool_result_bound_is_configurable(tmp_path: Path) -> None:
    """``limits.model_tool_result_chars`` bounds the MODEL lane, not the evidence lane."""

    from clio_agent.tools.execution import tool_workspace_context
    from clio_agent.tools.mcp_result_projection import (
        bounded_model_tool_result,
        model_tool_result_chars,
    )

    set_config("limits", {"model_tool_result_chars": 900})
    assert model_tool_result_chars() == 900

    with tool_workspace_context(str(tmp_path)):
        bounded = bounded_model_tool_result("x" * 5_000)
    assert len(bounded) <= 900
    assert bounded.startswith("[clio: result_spilled] This result is 5,000 characters")
    assert "more than the 900 shown to you" in bounded


def test_model_and_evidence_tool_result_bounds_are_independent() -> None:
    """The two lanes hold distinct keys: neither can silently redirect the other."""

    from clio_agent.gact.evidence import _bounded_tool_call_result
    from clio_agent.tools.mcp_result_projection import model_tool_result_chars

    set_config("limits", {"model_tool_result_chars": 900, "tool_result_chars": 40})
    assert model_tool_result_chars() == 900

    bounded = _bounded_tool_call_result("y" * 5_000)
    assert bounded["truncated"] is True
    assert len(bounded["preview"]) <= 40


# Child inference budgets apply only during model calls, never whole turns.


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [None, 6])
async def test_child_inference_concurrency_uses_the_resolved_cap(
    arc: Any,
    tmp_path: Path,
    configured: int | None,
) -> None:
    from clio_agent.gact.task_access import TaskCaller
    from tests.test_arc.test_conversation_owner_contracts import create_environment

    if configured is not None:
        set_config("agent_tasks", {"max_concurrent": configured})
    expected = 3 if configured is None else configured
    _app, owner, root = await create_environment(arc, tmp_path)
    caller = TaskCaller(root.owner.session_id, "human")
    children = [
        await owner.spawn(caller, f"Child {index}", "worker", f"budget-{index}")
        for index in range(expected + 1)
    ]
    owner.start()
    full, release = asyncio.Event(), asyncio.Event()
    entered: set[str] = set()
    active, maximum = 0, 0

    async def model_call(sid: str) -> None:
        nonlocal active, maximum
        async with owner.inference_slot(sid):
            entered.add(sid)
            active += 1
            maximum = max(maximum, active)
            if active == expected:
                full.set()
            try:
                await release.wait()
            finally:
                active -= 1

    models = [asyncio.create_task(model_call(child["session_id"])) for child in children]
    ready = asyncio.create_task(full.wait())
    try:
        completed, _pending = await asyncio.wait(
            [ready, *models], return_when=asyncio.FIRST_COMPLETED
        )
        if ready not in completed:
            await next(
                iter(completed)
            )  # Surface a worker setup/error rather than hang on readiness.
            pytest.fail("An inference worker retired before the configured capacity was occupied")
        assert len(entered) == expected
        async with owner.inference_slot(root.owner.session_id):
            assert active == expected  # Child budget does not become a new root-session cap.
        release.set()
        await asyncio.gather(*models)
        assert entered == {child["session_id"] for child in children}
        assert maximum == expected and active == 0
    finally:
        release.set()
        ready.cancel()
        await asyncio.gather(ready, return_exceptions=True)
        await asyncio.gather(*models, return_exceptions=True)
        await owner.shutdown()
        await owner.recovery.drain()


# --------------------------------------------------------------------------- #
# Auto-compaction default (gact/context_preferences_types.py)
# --------------------------------------------------------------------------- #


def test_autocompact_default_has_one_owner() -> None:
    """The wire-model default and the ``autocompact.pct`` fallback share a constant."""

    from clio_agent.gact.context_preferences_types import (
        DEFAULT_AUTOCOMPACT_PCT,
        ContextPreferences,
    )
    from clio_agent.gact.runtime.context_tokens import _autocompact_threshold

    assert ContextPreferences(session_id="s").autocompact_pct == DEFAULT_AUTOCOMPACT_PCT
    assert _autocompact_threshold() == DEFAULT_AUTOCOMPACT_PCT

    set_config("autocompact", {"pct": 0.7})
    assert _autocompact_threshold() == 0.7
    # An out-of-range configured value falls back to the same single constant.
    set_config("autocompact", {"pct": 5.0})
    assert _autocompact_threshold() == DEFAULT_AUTOCOMPACT_PCT


# --------------------------------------------------------------------------- #
# clio-core write retry ladder (arc/clio_core_retry.py)
# --------------------------------------------------------------------------- #


class _RefusingTag:
    """A CTE tag handle that refuses every write."""

    def __init__(self) -> None:
        self.calls = 0

    def PutBlob(self, name: str, payload: bytes, flags: int) -> None:  # noqa: N802
        self.calls += 1
        raise RuntimeError("PutBlob rc=13")


def test_clio_core_write_retry_ladder_is_configurable() -> None:
    """``arc.clio_core_write_retry.*`` owns the bounded write-retry ladder."""

    from clio_agent.arc import clio_core_retry

    set_config(
        "arc",
        {"clio_core_write_retry": {"attempts": 2, "first_delay_s": 0.0, "backoff_factor": 1.0}},
    )
    assert clio_core_retry.write_retry_attempts() == 2
    assert clio_core_retry.write_retry_first_delay_s() == 0.0
    assert clio_core_retry.write_retry_backoff_factor() == 1.0

    tag = _RefusingTag()
    try:
        with pytest.raises(RuntimeError, match="rc=13"):
            clio_core_retry.put_blob_with_retry(tag, "blob", b"payload")
        assert tag.calls == 2
        lost = clio_core_retry.last_lost_put_write()
        assert lost is not None and lost.name == "blob"
    finally:
        clio_core_retry._reset_put_write_health_for_tests()


@pytest.mark.parametrize(
    ("key", "env"),
    [
        ("tools.mcp.setup_timeout_s", "CLIO_MCP_SETUP_TIMEOUT_S"),
        ("tools.mcp.cold_spawn_runaway_s", "CLIO_MCP_COLD_SPAWN_RUNAWAY_S"),
        ("tools.mcp.launcher_cache_lock_timeout_s", "CLIO_MCP_LAUNCHER_CACHE_LOCK_TIMEOUT_S"),
        ("limits.mcp_reconnect_timeout_s", "CLIO_GACT_MCP_RECONNECT_TIMEOUT_S"),
    ],
)
def test_a_removed_fixed_mcp_deadline_key_is_a_typed_error(
    monkeypatch: pytest.MonkeyPatch, key: str, env: str
) -> None:
    """The fixed MCP deadlines are gone (#1577): a leftover one is refused, never ignored."""
    from clio_agent.errors import RemovedConfigKeyError
    from clio_agent.removed_config_keys import reject_removed_config_keys

    monkeypatch.setenv(env, "10")
    with pytest.raises(RemovedConfigKeyError) as err:
        reject_removed_config_keys()
    assert err.value.key == key
    assert "progress-based waits" in str(err.value)


@pytest.mark.parametrize(
    "where",
    ["env", "file"],
)
def test_the_removed_table_query_timeout_key_is_a_typed_error(
    monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """The fixed table-query timeout is gone (#1577): a leftover one is refused, never ignored."""
    from clio_agent.errors import RemovedConfigKeyError
    from clio_agent.removed_config_keys import reject_removed_config_keys

    if where == "env":
        monkeypatch.setenv("CLIO_ARTIFACTS_TABLE_QUERY_TIMEOUT_S", "10")
    else:
        set_config("artifacts", {"table_query_timeout_s": 10.0})
    with pytest.raises(RemovedConfigKeyError) as err:
        reject_removed_config_keys()
    assert err.value.key == "artifacts.table_query_timeout_s"
    assert err.value.error_type == "config_key_removed"
    assert "progress-based wait" in str(err.value)
