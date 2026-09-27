"""#1455: ``no_resolvable_agent`` states the observed cause, never a generic blame.

The collaborator's first message after re-signing in to Claude read "No
resolvable Agent Blueprint for this session; install the default registry or
activate an Agent Blueprint", and the resend worked. That error named no
blueprint and no fact. These tests pin each typed ``details.reason``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.agents import resolution
from clio_agent.gact.agents import unresolved_blueprint as unresolved
from clio_agent.gact.agents.unresolved_blueprint import (
    UNRESOLVED_BLUEPRINT_REASONS,
    diagnose_unresolved_blueprint,
    no_resolvable_error,
)


def _agent(agent_id: str, *, enabled: bool = True) -> Any:
    return SimpleNamespace(id=agent_id, enabled=enabled)


@pytest.fixture
def app() -> Any:
    return SimpleNamespace(state=SimpleNamespace(blueprint_resolution_reasons={}))


@pytest.fixture
def bound(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Bind the session to blueprint ``factorio``; tests set what a re-read sees."""

    seen: dict[str, Any] = {
        "row": SimpleNamespace(id="factorio", enabled=True, validation_errors=[]),
        "agents": [_agent("main")],
    }
    monkeypatch.setattr(
        resolution, "_runtime_effective_agent_blueprint_id", lambda app, sid: "factorio"
    )
    monkeypatch.setattr(
        resolution, "_runtime_effective_agent_blueprint_path", lambda app, sid: None
    )

    def _row(app: Any, sid: str, blueprint_id: str, path: Any) -> Any:
        if isinstance(seen["row"], Exception):
            raise seen["row"]
        return seen["row"]

    monkeypatch.setattr(unresolved, "_blueprint_row", _row)
    monkeypatch.setattr(
        resolution,
        "_runtime_active_agent_blueprint_rows",
        lambda app, session_id="": seen["agents"],
    )
    return seen


def test_missing_blueprint_is_named(app: Any, bound: dict[str, Any]) -> None:
    bound["row"] = None

    info = no_resolvable_error(app, "sess_1", "main")

    assert info.error == "no_resolvable_agent"
    assert info.details["reason"] == "blueprint_not_found"
    assert info.details["blueprint_id"] == "factorio"
    assert "'factorio' is not installed here" in info.message


def test_disabled_blueprint_reports_its_first_validation_error(
    app: Any, bound: dict[str, Any]
) -> None:
    bound["row"] = SimpleNamespace(
        id="factorio", enabled=False, validation_errors=["root_expert not found: main"]
    )

    info = no_resolvable_error(app, "sess_1", "main")

    assert info.details["reason"] == "blueprint_disabled"
    assert info.message.endswith("is disabled: root_expert not found: main")


def test_blueprint_that_loads_on_recheck_says_so_instead_of_blaming_it(
    app: Any, bound: dict[str, Any]
) -> None:
    """The #1455 shape: resolution read nothing at turn start, the resend
    worked. A re-read that finds the root enabled reports exactly that."""

    info = no_resolvable_error(app, "sess_1", "main")

    assert info.details["reason"] == "blueprint_resolved_on_recheck"
    assert "loads now" in info.message
    assert "install the default registry" not in info.message


def test_agent_outside_the_blueprint_is_named(app: Any, bound: dict[str, Any]) -> None:
    info = no_resolvable_error(app, "sess_1", "reviewer")

    assert info.details["reason"] == "agent_not_in_blueprint"
    assert info.details["enabled_agent_ids"] == ["main"]


def test_blueprint_with_no_enabled_root(app: Any, bound: dict[str, Any]) -> None:
    bound["agents"] = [_agent("main", enabled=False)]

    assert diagnose_unresolved_blueprint(app, "sess_1", "main")["reason"] == (
        "blueprint_has_no_enabled_root"
    )


def test_discovery_failure_is_reported_not_raised(app: Any, bound: dict[str, Any]) -> None:
    bound["row"] = OSError("permission denied")

    facts = diagnose_unresolved_blueprint(app, "sess_1", "main")

    assert facts["reason"] == "blueprint_discovery_failed"
    assert "permission denied" in facts["error"]


def test_every_reason_is_catalogued(app: Any, bound: dict[str, Any]) -> None:
    for row, agents, agent_id in (
        (None, [], "main"),
        (SimpleNamespace(id="factorio", enabled=False, validation_errors=[]), [], "main"),
        (bound["row"], [_agent("main")], "main"),
        (bound["row"], [_agent("main")], "reviewer"),
        (bound["row"], [], "main"),
    ):
        bound["row"], bound["agents"] = row, agents
        assert diagnose_unresolved_blueprint(app, "s", agent_id)["reason"] in (
            UNRESOLVED_BLUEPRINT_REASONS
        )
