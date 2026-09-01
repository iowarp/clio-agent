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
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent import conf
from tests._config_layer import set_config


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
    from clio_agent.gact.protocol_v3 import CLIO_A2UI_CATALOG_ID

    return {
        "version": "v0.9.1",
        "createSurface": {"surfaceId": surface_id, "catalogId": CLIO_A2UI_CATALOG_ID},
    }


def _data_message(index: int, surface_id: str = "surface_1") -> dict[str, Any]:
    return {
        "version": "v0.9.1",
        "updateDataModel": {"surfaceId": surface_id, "path": "/counter", "value": index},
    }


def test_a2ui_message_retention_bound_is_configurable() -> None:
    """``gact.ledger_retention.a2ui_messages.max`` bounds retained surface messages."""

    from clio_agent.gact.a2ui import apply_batch, max_a2ui_messages

    set_config("gact", {"ledger_retention": {"a2ui_messages": {"max": 4}}})
    assert max_a2ui_messages() == 4

    messages = [_create_message()]
    messages.extend(_data_message(index) for index in range(10))
    surfaces, _ = apply_batch({}, "sess_cfg", messages)

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

    set_config("a2ui", {"max_message_bytes": 64})
    assert max_a2ui_message_bytes() == 64

    with pytest.raises(A2UIValidationError, match="byte limit"):
        validate_server_message(_create_message())


def test_a2ui_string_bound_is_configurable() -> None:
    """``a2ui.max_string_chars`` bounds any single string inside a payload."""

    from clio_agent.gact.a2ui import (
        A2UIValidationError,
        max_a2ui_string_chars,
        validate_server_message,
    )

    set_config("a2ui", {"max_string_chars": 8})
    assert max_a2ui_string_chars() == 8

    message = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [{"component": "Text", "text": "x" * 64}],
        },
    }
    with pytest.raises(A2UIValidationError, match="size limit"):
        validate_server_message(message)


# --------------------------------------------------------------------------- #
# Cold MCP mount readiness (gact/mcp_readiness.py)
# --------------------------------------------------------------------------- #


class _StubExecutor:
    """Minimal live-executor surface the readiness boundary drives."""

    def __init__(self, *, setup_timeout: float | None = None) -> None:
        if setup_timeout is not None:
            self._setup_timeout = setup_timeout
        self.prepared_timeouts: list[float] = []

    def merge_namespace_tools(self, namespace: str, tools: Any) -> None:
        return None

    def prepare_namespace(self, namespace: str, timeout: float) -> None:
        self.prepared_timeouts.append(timeout)


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


def test_mcp_mount_setup_timeout_fallback_resolves_from_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An executor without ``_setup_timeout`` falls back to ``tools.mcp.setup_timeout_s``."""

    from clio_agent.gact.mcp_readiness import mount_namespace_for_session
    from clio_agent.tools import mcp_discovery

    set_config("tools", {"mcp": {"setup_timeout_s": 2.5}})
    monkeypatch.setattr(mcp_discovery, "ensure_namespace", lambda namespace, spec: {})

    executor = _StubExecutor()  # deliberately no _setup_timeout attribute
    assert mount_namespace_for_session(executor, "ns", object()) == {}
    # First attempt multiplier is 1.0, so the configured base surfaces verbatim.
    assert executor.prepared_timeouts == [2.5]


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
# Codex private credential homes (providers/codex_credential_home.py)
# --------------------------------------------------------------------------- #


def test_codex_credential_home_capacity_is_configurable() -> None:
    """``providers.codex.credential_home_capacity`` caps live private homes."""

    from clio_agent.providers.codex_credential_home import codex_credential_home_capacity

    set_config("providers", {"codex": {"credential_home_capacity": 2}})
    assert codex_credential_home_capacity() == 2


# --------------------------------------------------------------------------- #
# Blueprint source clone (gact/agent_blueprint_sources.py)
# --------------------------------------------------------------------------- #


def test_blueprint_source_clone_timeout_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """``gact.blueprint_source.clone_timeout_s`` bounds the source ``git clone``."""

    from clio_agent.gact import agent_blueprint_sources as module

    set_config("gact", {"blueprint_source": {"clone_timeout_s": 7.5}})
    assert module.blueprint_source_clone_timeout_s() == 7.5

    seen: dict[str, Any] = {}

    def _fake_run(command: list[str], **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise RuntimeError("clone stopped after capturing the timeout")

    monkeypatch.setattr(module.subprocess, "run", _fake_run)
    row = module.refresh_agent_blueprint_source(
        {"source": "https://example.invalid/repo.git", "ref": ""}
    )
    assert row["status"] == "error"
    assert seen["timeout"] == 7.5


# --------------------------------------------------------------------------- #
# SPOTTER clearance-event retention (gact/spotter_clearance.py)
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


@pytest.mark.asyncio
async def test_cancellation_grace_is_configurable() -> None:
    """``gact.cancellation_grace_s`` is the wait before the hard task cancel."""

    from clio_agent.gact.routes.session_cancellation import (
        _cancel_after_grace,
        cancellation_grace_s,
    )

    set_config("gact", {"cancellation_grace_s": 0.4})
    assert cancellation_grace_s() == 0.4

    async def _never() -> None:
        await asyncio.sleep(30)

    task = asyncio.create_task(_never())
    attempt: dict[str, Any] = {}
    app = SimpleNamespace(
        state=SimpleNamespace(cancel_flags={"sess_x"}, cancel_attempts={"sess_x": attempt})
    )
    started = time.monotonic()
    await _cancel_after_grace(app, task, "sess_x", attempt)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.35, "the configured grace must drive the wait, not the 0.1 literal"
    assert attempt["asyncio_task_cancel_sent"] is True
    assert task.cancelled() or task.cancelling()


# --------------------------------------------------------------------------- #
# Model-facing MCP result bound (tools/mcp_result_projection.py)
# --------------------------------------------------------------------------- #


def test_model_tool_result_bound_is_configurable() -> None:
    """``limits.model_tool_result_chars`` bounds the MODEL lane, not the evidence lane."""

    from clio_agent.tools.mcp_result_projection import (
        bounded_model_tool_result,
        model_tool_result_chars,
    )

    set_config("limits", {"model_tool_result_chars": 900})
    assert model_tool_result_chars() == 900

    bounded = bounded_model_tool_result("x" * 5_000)
    assert len(bounded) <= 900
    assert '"reason": "model_tool_result_oversize"' in bounded


def test_model_and_evidence_tool_result_bounds_are_independent() -> None:
    """The two lanes hold distinct keys: neither can silently redirect the other."""

    from clio_agent.gact.evidence import _bounded_tool_call_result
    from clio_agent.tools.mcp_result_projection import model_tool_result_chars

    set_config("limits", {"model_tool_result_chars": 900, "tool_result_chars": 40})
    assert model_tool_result_chars() == 900

    bounded = _bounded_tool_call_result("y" * 5_000)
    assert bounded["truncated"] is True
    assert len(bounded["preview"]) <= 40


# --------------------------------------------------------------------------- #
# Child-task concurrency (gact/turn_spawn_executor.py)
# --------------------------------------------------------------------------- #


def test_agent_task_concurrency_uses_the_resolved_cap() -> None:
    """``agent_tasks.max_concurrent`` reaches the per-depth pool, with one default."""

    from clio_agent.gact.turn_spawn_executor import (
        DEFAULT_MAX_CONCURRENT_AGENT_TASKS,
        agent_task_executor_for_depth,
        install_agent_task_executor,
        shutdown_agent_task_executors,
    )

    set_config("agent_tasks", {"max_concurrent": 6})
    app = SimpleNamespace(state=SimpleNamespace())
    install_agent_task_executor(app)
    try:
        assert app.state.max_concurrent_agent_tasks == 6
        assert agent_task_executor_for_depth(app, 1)._max_workers == 6
    finally:
        shutdown_agent_task_executors(app)

    # A pool built before install() falls back to the SAME named default the
    # resolver documents -- not a second, drifting literal.
    bare = SimpleNamespace(
        state=SimpleNamespace(agent_task_executors={}, agent_task_executor_lock=None)
    )
    import threading

    bare.state.agent_task_executor_lock = threading.Lock()
    pool = agent_task_executor_for_depth(bare, 1)
    try:
        assert pool._max_workers == DEFAULT_MAX_CONCURRENT_AGENT_TASKS
    finally:
        shutdown_agent_task_executors(bare)


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


# --------------------------------------------------------------------------- #
# Blueprint text-file limit prose (gact/routes/blueprint_file_write.py)
# --------------------------------------------------------------------------- #


def test_blueprint_too_large_message_is_derived_from_the_limit() -> None:
    """The 413 prose is computed from the enforced byte limit, never restated."""

    from clio_agent.gact.agent_blueprint_files import _BLUEPRINT_TEXT_FILE_LIMIT_BYTES
    from clio_agent.gact.routes.blueprint_file_write import _too_large_message

    expected_mib = _BLUEPRINT_TEXT_FILE_LIMIT_BYTES // (1024 * 1024)
    assert f"{expected_mib} MiB" in _too_large_message()
