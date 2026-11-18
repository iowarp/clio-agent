"""A saved connector connection follows its catalog entry when a release changes it."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.connectors import entries, materialize, refresh, secrets, state
from clio_agent.gact import connectors_service as service
from clio_agent.gact.mcp_user_configuration import get_user_mcp_server, set_user_mcp_server

OLD_ARGS = ["-B", "/an/older/server.py", "--mode", "secret"]


@pytest.fixture
def enabled(fixture_catalog: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """``fixture-secret`` enabled with a value and a secret, its saved copy from an older entry."""

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    found = entries.load_catalog().entries["fixture-secret"]
    state.update("fixture-secret", enabled=True, values={"where": "node08"})
    secrets.save("fixture-secret", {"token": "hidden-value"})
    service.save_connection(found, state.choice("fixture-secret"))
    older = {**get_user_mcp_server("fixture-secret"), "args": OLD_ARGS}  # type: ignore[dict-item]
    set_user_mcp_server("fixture-secret", older, label=found.name)
    return found


def test_a_stale_copy_is_rewritten_keeping_values_and_secrets(enabled: Any) -> None:
    events: list[str] = []
    trace_event = refresh.trace.event
    refresh.trace.event = lambda _cat, fmt, *args: events.append(fmt % args)  # type: ignore[method-assign]
    try:
        saved = get_user_mcp_server("fixture-secret")
        assert refresh.drift(saved, enabled) == ("args",)
        changed = refresh.refresh(
            enabled, saved, lambda: service.save_connection(enabled, state.choice(enabled.id))
        )
    finally:
        refresh.trace.event = trace_event  # type: ignore[method-assign]
    assert changed == ("args",)
    assert events == [f"{refresh.CONNECTION_REFRESHED} connector=fixture-secret fields=args"]
    rewritten = get_user_mcp_server("fixture-secret")
    assert rewritten["args"] == materialize.declaration(enabled)["args"]
    assert state.choice("fixture-secret").values == {"where": "node08"}
    assert secrets.fill("fixture-secret", {"T": "${need.token}"}) == {"T": "hidden-value"}
    assert "hidden-value" not in repr(rewritten)
    # A copy that matches its entry is left alone.
    assert refresh.refresh(enabled, rewritten, lambda: pytest.fail("rewrote a current copy")) == ()


def test_conversations_run_the_entry_and_the_refresh_brings_the_copy_along(enabled: Any) -> None:
    """A conversation's server is the entry as it is now; the saved copy catches up."""

    saved = get_user_mcp_server("fixture-secret")
    current = materialize.declaration(enabled)["args"]
    assert materialize.fill_declaration(saved, from_entry=True)["args"] == current
    assert materialize.in_use(saved)["args"] == OLD_ARGS  # the sign-in path reads the copy
    refresh.refresh(
        enabled, saved, lambda: service.save_connection(enabled, state.choice(enabled.id))
    )
    assert materialize.in_use(get_user_mcp_server("fixture-secret"))["args"] == current


def test_the_extensions_listing_refreshes_an_enabled_connectors_copy(
    enabled: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_probe(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(service, "probe", no_probe)
    app = SimpleNamespace(state=SimpleNamespace(bus=None, sessions=None))
    asyncio.run(service.rows(app))
    assert get_user_mcp_server("fixture-secret")["args"] == materialize.declaration(enabled)["args"]


def test_a_disabled_or_foreign_connection_is_not_touched(enabled: Any) -> None:
    other = dataclasses.replace(enabled, id="fixture-plain")
    saved = get_user_mcp_server("fixture-secret")
    assert refresh.refresh(other, saved, lambda: pytest.fail("rewrote a foreign copy")) == ()
    assert refresh.refresh(enabled, None, lambda: pytest.fail("wrote with no copy")) == ()
