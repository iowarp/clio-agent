"""Tests for the ``clio-agent-gact`` deprecation alias (#830).

``clio-agent serve`` is the single front door. The legacy ``clio-agent-gact``
console script now resolves to :func:`clio_agent.gact.app.main_deprecated`,
which stays fully functional for one release (old installed launchers still
call it) but emits a one-line stderr deprecation notice before delegating to
the real :func:`clio_agent.gact.app.main`.
"""

from __future__ import annotations

from clio_agent.gact import app


def test_main_deprecated_warns_and_delegates(monkeypatch, capsys):
    """The alias prints the deprecation notice to stderr and calls ``main`` once."""
    calls: list[int] = []
    monkeypatch.setattr(app, "main", lambda: calls.append(1))

    app.main_deprecated()

    assert calls == [1], "main_deprecated must delegate to main exactly once"
    captured = capsys.readouterr()
    assert "clio-agent-gact is deprecated" in captured.err
    assert "clio-agent serve" in captured.err
    # The notice is advisory only — it must not pollute stdout.
    assert captured.out == ""


def test_main_deprecated_propagates_main_failure(monkeypatch):
    """If the delegated ``main`` raises, the alias does not swallow it."""

    def _boom() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(app, "main", _boom)

    try:
        app.main_deprecated()
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:  # pragma: no cover - defensive
        raise AssertionError("main_deprecated must propagate main's exception")
