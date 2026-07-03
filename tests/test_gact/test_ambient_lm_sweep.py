"""Ambient ``dspy.settings.lm`` call-site sweep (per-expert-provider design §6, step 7).

Every runtime helper that reads ``dspy.settings.lm`` must either resolve the LM
bound by the active profile's ``dspy.context`` (expert/main) or, when it falls
through to the process boot-default LM, record a structured ``ambient_lm_default``
reason so the miss is *queryable* — never a silent dependency on the global
default the per-expert design removes.

These tests prove, for the owner guard (:mod:`clio_agent.gact.runtime.ambient_lm`)
and each swept call site:

* **bound context** — inside ``dspy.context(lm=X)`` the helper uses ``X`` and
  records NOTHING (single-default-LM baseline intact);
* **ambient fallback** — with no context bound the helper still returns the boot
  default AND records the structured catalog reason (asserted, not a bare
  fallback);
* **concurrency** — a bound thread and an ambient thread resolve independently
  with no cross-talk (ContextVar-scoped detection).
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import dspy
import pytest
from dspy.dsp.utils.settings import main_thread_config

from clio_agent.gact import context as _ctx
from clio_agent.gact.runtime import ambient_lm
from clio_agent.gact.runtime.ambient_lm import (
    AMBIENT_LM_FALLBACK_REASON,
    active_lm,
    ambient_lm_fallbacks,
    record_ambient_fallback,
    resolve_active_lm,
)
from clio_agent.gact.runtime.capabilities import _STREAM_FALLBACK_REASON_DEFINITIONS
from clio_agent.gact.streaming import (
    _AMBIENT_LM_FALLBACK_REASON_DEFINITIONS,
    _ambient_lm_fallback_payload,
)

# --------------------------------------------------------------------------- #
# helpers / fixtures
# --------------------------------------------------------------------------- #


def _app() -> Any:
    """A minimal FastAPI-shaped stub carrying only ``.state`` (an attr bag)."""
    return SimpleNamespace(state=SimpleNamespace())


class _LM:
    """A stand-in dspy LM object (never called; only its attrs are read)."""

    def __init__(self, model: str = "m", **attrs: Any) -> None:
        self.model = model
        self.history: list[Any] = attrs.pop("history", [])
        for key, value in attrs.items():
            setattr(self, key, value)


@pytest.fixture
def boot_default(monkeypatch: pytest.MonkeyPatch) -> _LM:
    """Install a process boot-default LM (``main_thread_config['lm']``) with NO
    active ``dspy.context`` — i.e. exactly the ambient state.

    Sets the global via ``main_thread_config`` (not ``dspy.configure``) so the test
    never contends with dspy's single-owner ``configure`` thread guard.
    """
    lm = _LM("boot-default")
    monkeypatch.setitem(main_thread_config, "lm", lm)
    return lm


@pytest.fixture
def session_ctx(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Bind an app + session id on the runtime context so record resolution finds them."""
    app = _app()
    app_token = _ctx.set_app(app)
    sid_token = _ctx.set_session_id("sess-1")
    yield app
    _ctx.reset(sid_token)
    _ctx.reset(app_token)


# --------------------------------------------------------------------------- #
# detection primitives
# --------------------------------------------------------------------------- #


def test_context_lm_bound_detection(boot_default: _LM) -> None:
    """``_context_lm_bound`` is False ambient, True inside ``dspy.context(lm=...)``."""
    assert ambient_lm._context_lm_bound() is False
    with dspy.context(lm=_LM("bound")):
        assert ambient_lm._context_lm_bound() is True
    assert ambient_lm._context_lm_bound() is False


def test_active_lm_reports_bound_and_ambient(boot_default: _LM) -> None:
    """``active_lm`` returns the boot default+ambient=True with no context, the
    bound LM+ambient=False inside one."""
    lm, ambient = active_lm()
    assert lm is boot_default and ambient is True
    bound = _LM("bound")
    with dspy.context(lm=bound):
        lm2, ambient2 = active_lm()
        assert lm2 is bound and ambient2 is False


# --------------------------------------------------------------------------- #
# resolve_active_lm + the ledger
# --------------------------------------------------------------------------- #


def test_explicit_lm_wins_and_records_nothing() -> None:
    """An explicit LM is returned as-is and never triggers an ambient record."""
    app = _app()
    explicit = _LM("explicit")
    got = resolve_active_lm(site="t.explicit", explicit=explicit, app=app, sid="s")
    assert got is explicit
    assert ambient_lm_fallbacks(app) == {}


def test_bound_context_records_nothing(boot_default: _LM) -> None:
    """Inside the active profile's ``dspy.context`` the bound LM is used, no record."""
    app = _app()
    bound = _LM("bound")
    with dspy.context(lm=bound):
        got = resolve_active_lm(site="t.bound", app=app, sid="s")
    assert got is bound
    assert ambient_lm_fallbacks(app) == {}


def test_ambient_fallback_records_structured_reason(boot_default: _LM) -> None:
    """An ambient read returns the boot default AND records the catalog reason."""
    app = _app()
    got = resolve_active_lm(site="t.ambient", app=app, sid="s")
    assert got is boot_default  # baseline value preserved
    entries = ambient_lm_fallbacks(app)["s"]
    assert len(entries) == 1
    payload = entries[0]
    # A structured catalog payload, not a bare marker.
    assert payload["reason"] == AMBIENT_LM_FALLBACK_REASON
    assert payload["message"] == "t.ambient"
    assert payload["category"] == "provider_binding"
    assert "recovery_actions" in payload and payload["recovery_actions"]


def test_record_dedups_same_site_and_caps(session_ctx: Any) -> None:
    """Consecutive same-site records collapse to one; distinct sites accumulate."""
    app = session_ctx
    for _ in range(5):
        record_ambient_fallback("site.a", app=app, sid="s")
    record_ambient_fallback("site.b", app=app, sid="s")
    record_ambient_fallback("site.a", app=app, sid="s")
    messages = [e["message"] for e in ambient_lm_fallbacks(app)["s"]]
    # 5x a -> one a; then b; then a again (site changed) -> a,b,a.
    assert messages == ["site.a", "site.b", "site.a"]


def test_record_off_session_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no reachable app/session the record is logged, never raised."""
    monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: None)
    monkeypatch.setattr("clio_agent.gact.context.active_session_id", lambda: "")
    # Must not raise even though nothing can be attributed.
    record_ambient_fallback("t.offsession")


def test_reason_lives_in_sibling_catalog_not_streaming_set() -> None:
    """The ambient reason is a typed catalog entry — in the dedicated sibling
    catalog, NOT the audited client-facing stream_fallback set (which must stay a
    closed set of live-streaming fallbacks)."""
    assert AMBIENT_LM_FALLBACK_REASON in _AMBIENT_LM_FALLBACK_REASON_DEFINITIONS
    assert AMBIENT_LM_FALLBACK_REASON not in _STREAM_FALLBACK_REASON_DEFINITIONS


def test_ambient_payload_rejects_unknown_reason() -> None:
    """Like the stream_fallback payload, an unknown reason is rejected (no bare fallback)."""
    with pytest.raises(ValueError, match="Unknown ambient LM fallback reason"):
        _ambient_lm_fallback_payload("not_a_real_reason")


# --------------------------------------------------------------------------- #
# swept call site: context_tokens
# --------------------------------------------------------------------------- #


def test_last_prompt_tokens_ambient_records(boot_default: _LM, session_ctx: Any) -> None:
    from clio_agent.gact.runtime.context_tokens import _last_prompt_tokens

    _last_prompt_tokens()
    sites = [e["message"] for e in ambient_lm_fallbacks(session_ctx).get("sess-1", [])]
    assert "context_tokens._last_prompt_tokens" in sites


def test_last_prompt_tokens_bound_records_nothing(session_ctx: Any) -> None:
    from clio_agent.gact.runtime.context_tokens import _last_prompt_tokens

    with dspy.context(lm=_LM("bound")):
        _last_prompt_tokens()
    assert ambient_lm_fallbacks(session_ctx) == {}


def test_estimate_text_tokens_ambient_records(boot_default: _LM, session_ctx: Any) -> None:
    from clio_agent.gact.runtime.context_tokens import _estimate_text_tokens

    _estimate_text_tokens("some text to estimate")
    sites = [e["message"] for e in ambient_lm_fallbacks(session_ctx).get("sess-1", [])]
    assert "context_tokens._estimate_text_tokens" in sites


# --------------------------------------------------------------------------- #
# swept call site: agents.runtime._summarize_segments_llm
# --------------------------------------------------------------------------- #


def _seg(text: str) -> Any:
    return SimpleNamespace(kind="thought", content={"text": text})


def test_summarize_bound_uses_bound_lm_no_record(
    monkeypatch: pytest.MonkeyPatch, session_ctx: Any
) -> None:
    from clio_agent.gact.agents import runtime as agents_runtime

    captured: dict[str, Any] = {}

    class _FakePredict:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def __call__(self, *, prior_context: str, lm: Any = None) -> Any:
            captured["lm"] = lm
            return SimpleNamespace(summary="COMPACTED")

    monkeypatch.setattr(dspy, "Predict", _FakePredict)
    bound = _LM("bound")
    with dspy.context(lm=bound):
        out = agents_runtime._summarize_segments_llm([_seg("a"), _seg("b")])
    assert out == "COMPACTED"
    assert captured["lm"] is bound  # ran on the bound profile LM explicitly
    assert ambient_lm_fallbacks(session_ctx) == {}


def test_summarize_ambient_records_and_passes_boot_default(
    monkeypatch: pytest.MonkeyPatch, boot_default: _LM, session_ctx: Any
) -> None:
    from clio_agent.gact.agents import runtime as agents_runtime

    captured: dict[str, Any] = {}

    class _FakePredict:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def __call__(self, *, prior_context: str, lm: Any = None) -> Any:
            captured["lm"] = lm
            return SimpleNamespace(summary="OK")

    monkeypatch.setattr(dspy, "Predict", _FakePredict)
    out = agents_runtime._summarize_segments_llm([_seg("a")])
    assert out == "OK"
    assert captured["lm"] is boot_default
    sites = [e["message"] for e in ambient_lm_fallbacks(session_ctx)["sess-1"]]
    assert "agents.runtime._summarize_segments_llm" in sites


# --------------------------------------------------------------------------- #
# swept call site: globals._active_lm_last_reasoning
# --------------------------------------------------------------------------- #


def test_active_lm_last_reasoning_bound_vs_ambient(
    monkeypatch: pytest.MonkeyPatch, session_ctx: Any
) -> None:
    from clio_agent.gact.runtime.globals import _active_lm_last_reasoning

    # bound: reads the bound LM's stashed reasoning, no record.
    with dspy.context(lm=_LM("bound", _clio_last_reasoning="BOUND-COT")):
        assert _active_lm_last_reasoning() == "BOUND-COT"
    assert ambient_lm_fallbacks(session_ctx) == {}

    # ambient: reads the boot default's stash AND records.
    monkeypatch.setitem(main_thread_config, "lm", _LM("boot", _clio_last_reasoning="AMB-COT"))
    assert _active_lm_last_reasoning() == "AMB-COT"
    sites = [e["message"] for e in ambient_lm_fallbacks(session_ctx)["sess-1"]]
    assert "globals._active_lm_last_reasoning" in sites


# --------------------------------------------------------------------------- #
# swept call site: app._current_lm_model_id
# --------------------------------------------------------------------------- #


def test_current_lm_model_id_bound_vs_ambient(
    monkeypatch: pytest.MonkeyPatch, session_ctx: Any
) -> None:
    from clio_agent.gact.app import _current_lm_model_id

    with dspy.context(lm=_LM("bound-model")):
        assert _current_lm_model_id() == "bound-model"
    assert ambient_lm_fallbacks(session_ctx) == {}

    monkeypatch.setitem(main_thread_config, "lm", _LM("ambient-model"))
    assert _current_lm_model_id() == "ambient-model"  # baseline value preserved
    sites = [e["message"] for e in ambient_lm_fallbacks(session_ctx)["sess-1"]]
    assert "app._current_lm_model_id" in sites


# --------------------------------------------------------------------------- #
# swept call site: usage rollup
# --------------------------------------------------------------------------- #


def test_all_known_lms_ambient_records(boot_default: _LM, session_ctx: Any) -> None:
    from clio_agent.gact.usage import _all_known_lms

    lms = _all_known_lms(session_ctx)
    assert boot_default in lms  # baseline: the global LM is still gathered
    sites = [e["message"] for e in ambient_lm_fallbacks(session_ctx).get("sess-1", [])]
    assert "usage._all_known_lms" in sites


def test_usage_from_dspy_history_ambient_records(boot_default: _LM, session_ctx: Any) -> None:
    from clio_agent.gact.usage import _usage_from_dspy_history

    _usage_from_dspy_history()  # empty history -> {}, but the ambient read is flagged
    sites = [e["message"] for e in ambient_lm_fallbacks(session_ctx).get("sess-1", [])]
    assert "usage._usage_from_dspy_history" in sites


# --------------------------------------------------------------------------- #
# backward-compat
# --------------------------------------------------------------------------- #


def test_backward_compat_boot_default_still_resolves(boot_default: _LM) -> None:
    """With a single boot default and no context, resolution returns it unchanged —
    the recorded reason is additive, not a behavior change (RULE 2)."""
    app = _app()
    got = resolve_active_lm(site="t.compat", app=app, sid="s")
    assert got is boot_default
    # Recording is purely additive.
    assert ambient_lm_fallbacks(app)["s"][0]["reason"] == AMBIENT_LM_FALLBACK_REASON


# --------------------------------------------------------------------------- #
# concurrency (ContextVar-scoped, no cross-talk)
# --------------------------------------------------------------------------- #


@pytest.mark.concurrency
def test_bound_and_ambient_threads_resolve_independently(boot_default: _LM) -> None:
    """A bound thread and an ambient thread resolve with no cross-talk.

    The bound thread's ``dspy.context`` is ContextVar-scoped to that thread, so the
    ambient thread never sees it (and vice versa): the bound thread records nothing,
    the ambient thread records exactly its site.
    """
    app = _app()
    ambient_lm_fallbacks(app)  # pre-create the ledger so both threads see one dict
    results: dict[str, Any] = {}
    barrier = threading.Barrier(2)
    bound_lm = _LM("thread-bound")

    def _bound() -> None:
        barrier.wait()
        with dspy.context(lm=bound_lm):
            results["bound"] = resolve_active_lm(site="conc.bound", app=app, sid="sA")

    def _ambient() -> None:
        barrier.wait()
        results["ambient"] = resolve_active_lm(site="conc.ambient", app=app, sid="sB")

    threads = [threading.Thread(target=_bound), threading.Thread(target=_ambient)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results["bound"] is bound_lm
    assert results["ambient"] is boot_default
    ledger = ambient_lm_fallbacks(app)
    assert "sA" not in ledger  # bound thread recorded nothing
    assert [e["message"] for e in ledger["sB"]] == ["conc.ambient"]
