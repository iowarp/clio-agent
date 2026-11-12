"""``GET /v1/health`` says when the default agents could not be installed.

A fresh install on a machine without git could not clone the default marketplace:
every new conversation failed with ``agent 'clio/standard' is not installed`` while
health reported ``ready``. The row carries the install's own typed failure.
"""

from __future__ import annotations

from types import SimpleNamespace

from clio_agent.gact.health_folds import default_marketplace_rows
from clio_agent.runtime.status import IntegrationState


class _Task:
    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


def _app(*, done: bool, error: dict | None) -> SimpleNamespace:
    service = SimpleNamespace(ready_error=error)
    return SimpleNamespace(
        state=SimpleNamespace(plugin_service=service, plugin_sync_task=_Task(done))
    )


def test_no_row_while_the_install_still_runs() -> None:
    assert default_marketplace_rows(_app(done=False, error=None)) == []
    assert default_marketplace_rows(SimpleNamespace(state=SimpleNamespace())) == []


def test_a_failed_install_degrades_health_with_its_reason() -> None:
    error = {
        "reason": "default_marketplace_unavailable",
        "source": "https://github.com/iowarp/clio-agent-marketplace.git",
        "message": "git init failed: git is not installed",
    }
    [row] = default_marketplace_rows(_app(done=True, error=error))
    assert row.name == "default_marketplace"
    assert row.state is IntegrationState.DEGRADED
    assert row.required  # a required degraded row makes overall_status "degraded"
    assert "git is not installed" in row.summary
    assert row.details["reason"] == "default_marketplace_unavailable"
    assert row.config_source == error["source"]


def test_an_installed_marketplace_is_ready() -> None:
    [row] = default_marketplace_rows(_app(done=True, error=None))
    assert row.state is IntegrationState.READY
