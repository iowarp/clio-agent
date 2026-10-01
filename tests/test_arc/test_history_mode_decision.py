"""History mode is decided once per process, only from the platform missing clio-core.

The owner's one sanctioned fallback: when the clio-core binding is not installed on this
platform, CLIO runs on DSPy ``History``, loudly. Every other clio-core failure stays a
typed error, and the mode never switches mid-process.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from clio_agent.arc import history_mode
from clio_agent.arc.init_degradation import ArcStoreUnavailableError


@pytest.fixture
def no_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history_mode, "binding_present", lambda: False)


def test_the_binding_present_is_clio_core_mode() -> None:
    mode = history_mode.resolve()

    assert (mode.mode, mode.is_history) == ("clio_core", False)
    assert history_mode.active() is False


@pytest.mark.history_mode
@pytest.mark.usefixtures("no_binding")
def test_a_missing_binding_is_history_mode_with_its_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    before = history_mode.entries()

    mode = history_mode.resolve()

    assert (mode.mode, mode.reason) == ("history", "clio_core_binding_absent")
    assert history_mode.active() is True
    assert history_mode.entries() == before + 1
    assert any("History mode" in r.getMessage() for r in caplog.records)


@pytest.mark.history_mode
@pytest.mark.usefixtures("no_binding")
def test_the_decision_is_made_once(monkeypatch: pytest.MonkeyPatch) -> None:
    history_mode.resolve()
    monkeypatch.setattr(history_mode, "binding_present", lambda: True)

    assert history_mode.resolve().is_history  # never switches mid-process


def test_the_binding_present_never_enters_history_after_a_failed_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A present binding whose build fails is a typed failure, never History mode."""
    from clio_agent.gact.runtime import globals as runtime_globals

    def _refused(**_kw: object) -> object:
        raise ArcStoreUnavailableError(error=RuntimeError("never bound port"), config_path="x")

    monkeypatch.setattr("clio_agent.arc.storage.make_arc_store", _refused)
    app = SimpleNamespace(state=SimpleNamespace(arc=None))

    with pytest.raises(ArcStoreUnavailableError):
        runtime_globals._process_arc(app)
    assert history_mode.active() is False


@pytest.mark.history_mode
@pytest.mark.usefixtures("no_binding")
def test_the_process_arc_is_none_in_history_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact.runtime import globals as runtime_globals

    def _must_not_build(**_kw: object) -> object:
        raise AssertionError("History mode never builds a clio-core store")

    monkeypatch.setattr("clio_agent.arc.storage.make_arc_store", _must_not_build)
    app = SimpleNamespace(state=SimpleNamespace(arc=None))

    assert runtime_globals._process_arc(app) is None


@pytest.mark.history_mode
@pytest.mark.usefixtures("no_binding")
def test_the_agent_builds_without_arc_in_history_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent import agent as agent_module

    def _must_not_build(**_kw: object) -> object:
        raise AssertionError("History mode never builds a clio-core store")

    monkeypatch.setattr(agent_module, "make_arc_store", _must_not_build)

    assert agent_module.ClioAgent._arc_for_mode(None, data_dir="unused") is None
    with pytest.raises(history_mode.HistoryModeUnsupportedError) as err:
        history_mode.require_arc(None, "ARC statistics")
    assert err.value.error_type == "history_mode_unsupported"


class _Node:
    def __init__(self, marked: bool) -> None:
        self._marked = marked

    def get_closest_marker(self, name: str) -> object:
        return object() if (self._marked and name == "history_mode") else None


@pytest.mark.history_mode
@pytest.mark.usefixtures("no_binding")
def test_the_guard_fails_an_unmarked_test_that_entered_history_mode() -> None:
    from tests._history_mode_guard import guard

    with pytest.raises(pytest.fail.Exception, match="entered History mode"):
        with guard(_Node(marked=False)):
            history_mode.resolve()
    assert history_mode.active() is False  # the decision never leaks past a test


@pytest.mark.history_mode
@pytest.mark.usefixtures("no_binding")
def test_the_guard_lets_a_marked_test_enter_history_mode() -> None:
    from tests._history_mode_guard import guard

    with guard(_Node(marked=True)):
        history_mode.resolve()
