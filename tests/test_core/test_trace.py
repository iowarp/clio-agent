"""Tests for the structured trace subsystem (clio_agent.runtime.trace)."""

from __future__ import annotations

import logging

import pytest

from clio_agent import conf
from clio_agent.runtime import trace


class _Capture(logging.Handler):
    """Collects emitted records (sidesteps propagate=False / caplog root capture)."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def tags(self) -> list[str]:
        # message looks like "⚑ TAG rest..." → return the TAG token
        out = []
        for r in self.records:
            parts = r.getMessage().split(" ", 2)
            out.append(parts[1] if len(parts) > 1 else "")
        return out


@pytest.fixture
def cap(monkeypatch):
    """Attach a capture handler to the clio_agent logger and reset gates after."""
    logger = logging.getLogger("clio_agent")
    handler = _Capture()
    logger.addHandler(handler)
    monkeypatch.setattr(logger, "level", logging.WARNING, raising=False)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        # restore default gating so later tests are unaffected
        trace.configure(level="low", only=[], install_handler=False)


def _emit_all() -> None:
    trace.event("SCHEMA-REPAIR", "e=%s", 1)
    trace.route("RAW-ROUTE", "r=%s", 2)
    trace.HF_ON and trace.hot("LM-CALL", "h=%s", 3)


class TestVerbosityLevels:
    def test_default_low_is_events_only(self, cap):
        trace.configure(level="low", only=[], install_handler=False)
        _emit_all()
        assert cap.tags() == ["SCHEMA-REPAIR"]

    def test_off_emits_nothing(self, cap):
        trace.configure(level="off", only=[], install_handler=False)
        _emit_all()
        assert cap.tags() == []

    def test_med_adds_routing(self, cap):
        trace.configure(level="med", only=[], install_handler=False)
        _emit_all()
        assert cap.tags() == ["SCHEMA-REPAIR", "RAW-ROUTE"]

    def test_high_enables_all(self, cap):
        trace.configure(level="high", only=[], install_handler=False)
        _emit_all()
        assert cap.tags() == ["SCHEMA-REPAIR", "RAW-ROUTE", "LM-CALL"]

    def test_unknown_level_falls_back_to_low(self, cap):
        trace.configure(level="bogus", only=[], install_handler=False)
        _emit_all()
        assert cap.tags() == ["SCHEMA-REPAIR"]


class TestOnlyWhitelist:
    def test_only_overrides_level(self, cap):
        # at off, a whitelisted tag still emits; non-whitelisted stays silent
        trace.configure(level="off", only=["lm_call"], install_handler=False)
        _emit_all()
        assert cap.tags() == ["LM-CALL"]

    def test_only_normalizes_dashes_and_case(self, cap):
        trace.configure(level="off", only=["RAW-ROUTE"], install_handler=False)
        _emit_all()
        assert cap.tags() == ["RAW-ROUTE"]


class TestDisableSemantics:
    def test_hf_guard_skips_arg_evaluation_when_off(self, cap):
        """The `HF_ON and hot(...)` idiom must not evaluate its args when off."""
        trace.configure(level="low", only=[], install_handler=False)  # HF off
        assert trace.HF_ON is False

        def boom():
            raise AssertionError("argument was evaluated despite HF_ON=False")

        # Must NOT raise: short-circuit skips the whole right operand.
        trace.HF_ON and trace.hot("LM-CALL", "%s", boom())
        assert cap.tags() == []

    def test_gates_are_plain_booleans(self, cap):
        trace.configure(level="high", only=[], install_handler=False)
        assert (trace.EVENT_ON, trace.ROUTE_ON, trace.HF_ON) == (True, True, True)
        trace.configure(level="off", only=[], install_handler=False)
        assert (trace.EVENT_ON, trace.ROUTE_ON, trace.HF_ON) == (False, False, False)


class TestLegacyBackCompat:
    def test_clio_log_lm_response_enables_tag(self, cap, monkeypatch, tmp_path):
        # point the config store at an empty dir so only env drives resolution
        monkeypatch.setattr(conf, "_STORE", conf.ConfigStore(home=tmp_path, cwd=tmp_path))
        monkeypatch.setenv("CLIO_LOG_LM_RESPONSE", "1")
        trace.configure(level="off", install_handler=False)  # resolves only from env/file
        trace.hot("LM-RESPONSE", "payload=%s", "x")
        assert cap.tags() == ["LM-RESPONSE"]


def test_message_format_includes_flag_and_args(cap):
    trace.configure(level="high", only=[], install_handler=False)
    trace.hot("LM-CALL", "sp_len=%d head=%r", 7, "abc")
    assert cap.records[0].getMessage() == "⚑ LM-CALL sp_len=7 head='abc'"
