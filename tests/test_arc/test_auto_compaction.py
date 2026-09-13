"""Acceptance: per-expert 90%-style auto-compaction (GOAL.md "Definition of done" #3).

Fires off the provider's exact ``prompt_tokens / context_window`` when the ratio
crosses a configurable threshold. Since #1339 the auto trigger is unified onto ONE
operation (``gact.compaction.compact_session_context``, ``trigger="auto"``): it still
folds the live ARC working set into one summary observation exactly as before
(``arc.summarize_segments`` collapses the scope -- the assertion these tests pin is
unchanged), but the SUMMARY TEXT now comes from that one operation's own LM call over
the session's gact ledger, not a direct ``_summarize_segments_llm(live)`` call over the
ARC segments. So the fake app here needs the full seam the operation touches
(``messages``/``agent``/``sessions.update``/``bus``/``memory_events``), not just
``arc``/``sessions.get`` -- see :func:`_full_plane_context` below.

``_last_prompt_tokens`` and the fake agent's ``_run_chat_agent`` are patched so the
trigger is deterministic and no real LM call is made; ``ctx.set_react_window``
(unpatched) still drives the window denominator exactly as before.
"""

from __future__ import annotations

import contextlib
import types
from datetime import datetime, timezone
from typing import Any, Iterator

import dspy

import clio_agent.gact.app as app
import clio_agent.gact.runtime.context_tokens as context_tokens
from clio_agent.gact import context as ctx
from clio_agent.gact.types import Message, Part, Tokens

from .conftest import make_react_agent

SID, SCOPE = "s1", "agentA"


def _populate(arc, scope=SCOPE):
    arc.append_segment(SID, scope, "thought", {"text": "T0"}, step=0)
    arc.append_segment(SID, scope, "tool_call", {"name": "a", "args": {}}, step=0)
    arc.append_segment(SID, scope, "observation", {"text": "O0"}, step=0)


class _FakeSessions:
    """Minimal ``app.state.sessions`` stand-in: ``.get`` (compact_session_context's
    404 check) + ``.update`` (``append_checkpoint``'s message_count bump)."""

    def __init__(self, rows: dict[str, Any]) -> None:
        self._rows = rows

    def get(self, sid: str) -> Any:
        return self._rows.get(sid)

    def update(self, sid: str, **_kwargs: Any) -> None:
        pass  # the acceptance contract here is the ARC fold, not the session row


class _FakeBus:
    """``app.state.bus`` stand-in: ``append_checkpoint`` publishes two events."""

    def __init__(self) -> None:
        self.published: list[Any] = []

    def publish(self, event: Any) -> None:
        self.published.append(event)


class _FakeAgent:
    """``app.state.agent`` stand-in: the ONE LM seam ``compact_session_context`` calls."""

    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.prompts: list[str] = []

    def _run_chat_agent(self, question: str, _session_id: str) -> str:
        self.prompts.append(question)
        return self.summary


def _seed_ledger_message(sid: str, text: str = "seed") -> Message:
    now = datetime.now(timezone.utc).isoformat()
    return Message(
        id="msg_seed",
        session_id=sid,
        role="user",
        created_at=now,
        updated_at=now,
        parts=[Part(id="part_seed", type="text", text=text)],
        tokens=Tokens(),
        stop_reason="end_turn",
    )


@contextlib.contextmanager
def _full_plane_context(
    arc_memory,
    *,
    session: str = SID,
    scope: str = SCOPE,
    window: int = 0,
    session_metadata: dict[str, Any] | None = None,
    summary: str = "COMPACT_SUMMARY",
) -> Iterator[_FakeAgent]:
    """The #1339 analog of ``conftest.live_plane_context``: the same runtime
    contextvars, plus the gact-level app state ``compact_session_context`` now needs
    (a one-message ledger so it never skips ``model_context_empty``, a fake LM agent,
    a bus, a memory-events store). Returns the fake agent so a test can inspect
    ``.prompts``."""

    fake_session = types.SimpleNamespace(metadata=session_metadata or {})
    fake_agent = _FakeAgent(summary)
    fake_app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            arc=arc_memory,
            sessions=_FakeSessions({session: fake_session}),
            messages={session: [_seed_ledger_message(session)]},
            agent=fake_agent,
            bus=_FakeBus(),
            memory_events={},
        )
    )
    app_token = ctx.set_app(fake_app)
    scope_token = ctx.set_react_scope(scope)
    session_token = ctx.set_react_session(session)
    window_token = ctx.set_react_window(window)
    try:
        yield fake_agent
    finally:
        ctx.reset(window_token)
        ctx.reset(session_token)
        ctx.reset(scope_token)
        ctx.reset(app_token)


def _patch_prompt_tokens(monkeypatch, prompt_tokens: int) -> None:
    # #1339: the auto trigger (gact.compaction.maybe_autocompact) resolves this
    # helper from its canonical owner module, context_tokens -- not the
    # gact.agents.runtime re-export shim the classic loop used, so patch it there.
    monkeypatch.setattr(context_tokens, "_last_prompt_tokens", lambda: prompt_tokens)


def test_fires_over_threshold(arc, monkeypatch):
    _patch_prompt_tokens(monkeypatch, prompt_tokens=900)  # 900/1000 = 0.90 >= 0.85 default
    _populate(arc)
    agent = make_react_agent()
    with _full_plane_context(arc, session=SID, scope=SCOPE, window=1000):
        agent._maybe_autocompact()
    # collapsed to a single summary observation
    assert arc.render_segments_keys(SID, SCOPE) == {"observation_0": "COMPACT_SUMMARY"}


def test_does_not_fire_under_threshold(arc, monkeypatch):
    _patch_prompt_tokens(monkeypatch, prompt_tokens=500)  # 0.50 < 0.85
    _populate(arc)
    agent = make_react_agent()
    before = arc.render_segments_keys(SID, SCOPE)
    with _full_plane_context(arc, session=SID, scope=SCOPE, window=1000):
        agent._maybe_autocompact()
    assert arc.render_segments_keys(SID, SCOPE) == before  # untouched


def test_session_can_disable_automatic_compaction(arc, monkeypatch):
    _patch_prompt_tokens(monkeypatch, prompt_tokens=900)
    _populate(arc)
    agent = make_react_agent()
    before = arc.render_segments_keys(SID, SCOPE)
    metadata = {
        "context_preferences": {
            "automatic_compaction": False,
            "autocompact_pct": 0.50,
        }
    }

    with _full_plane_context(
        arc,
        session=SID,
        scope=SCOPE,
        window=1000,
        session_metadata=metadata,
    ):
        agent._maybe_autocompact()

    assert arc.render_segments_keys(SID, SCOPE) == before


def test_session_threshold_overrides_deployment_default(arc, monkeypatch):
    monkeypatch.setenv("CLIO_AUTOCOMPACT_PCT", "0.95")
    _patch_prompt_tokens(monkeypatch, prompt_tokens=600)
    _populate(arc)
    agent = make_react_agent()
    metadata = {
        "context_preferences": {
            "automatic_compaction": True,
            "autocompact_pct": 0.50,
        }
    }

    with _full_plane_context(
        arc,
        session=SID,
        scope=SCOPE,
        window=1000,
        session_metadata=metadata,
    ):
        agent._maybe_autocompact()

    assert arc.render_segments_keys(SID, SCOPE) == {"observation_0": "COMPACT_SUMMARY"}


def test_threshold_is_env_configurable(arc, monkeypatch):
    monkeypatch.setenv("CLIO_AUTOCOMPACT_PCT", "0.50")
    _patch_prompt_tokens(monkeypatch, prompt_tokens=600)  # 0.60 >= 0.50 (would NOT fire at 0.85)
    _populate(arc)
    agent = make_react_agent()
    with _full_plane_context(arc, session=SID, scope=SCOPE, window=1000):
        agent._maybe_autocompact()
    assert arc.render_segments_keys(SID, SCOPE) == {"observation_0": "COMPACT_SUMMARY"}


def test_disabled_when_window_unknown(arc, monkeypatch):
    _patch_prompt_tokens(monkeypatch, prompt_tokens=9999)  # huge, but window=0 => no denominator
    _populate(arc)
    agent = make_react_agent()
    before = arc.render_segments_keys(SID, SCOPE)
    with _full_plane_context(arc, session=SID, scope=SCOPE, window=0):
        agent._maybe_autocompact()
    assert arc.render_segments_keys(SID, SCOPE) == before  # auto-compaction off


def test_skips_when_summary_llm_returns_empty(arc, monkeypatch):
    """#1339: an empty LM summary no longer aborts the fold -- ``compact_session_context``
    folds whatever text it got (empty or not); this pins that an empty ``summary``
    still lands as the fold text (no more "empty means skip the whole compaction"
    special case, since the checkpoint is the unit of work now, not the ARC fold
    alone -- see the module docstring)."""

    _patch_prompt_tokens(monkeypatch, prompt_tokens=900)
    _populate(arc)
    agent = make_react_agent()
    with _full_plane_context(arc, session=SID, scope=SCOPE, window=1000, summary=""):
        agent._maybe_autocompact()
    assert arc.render_segments_keys(SID, SCOPE) == {"observation_0": ""}


def test_last_prompt_tokens_falls_back_to_token_counter(monkeypatch):
    """Regression guard: when the provider reports no/zero prompt_tokens (the ALCF
    vLLM endpoint returns prompt_tokens:0), _last_prompt_tokens must fall back to a
    client-side token_counter over the last call's real messages — else
    auto-compaction never fires in production. Uses tiktoken offline (no network)."""
    fake_lm = type(
        "FakeLM",
        (),
        {
            "model": "gpt-3.5-turbo",  # tiktoken-native => offline count
            "history": [{"messages": [{"role": "user", "content": "the quick brown fox " * 30}]}],
        },
    )()
    with dspy.context(lm=fake_lm):
        # no usage_tracker installed -> must use the history/token_counter fallback
        n = app._last_prompt_tokens()
    assert n > 0, "fallback failed: a non-empty prompt must count > 0 tokens"


def test_last_prompt_tokens_fallback_when_tracker_reports_zero(monkeypatch):
    """Even with a usage tracker present, a 0 prompt_tokens must fall back."""
    fake_lm = type(
        "FakeLM",
        (),
        {
            "model": "gpt-3.5-turbo",
            "history": [{"messages": [{"role": "user", "content": "hello world " * 25}]}],
        },
    )()
    fake_tracker = type(
        "FakeTracker", (), {"usage_data": {"gpt-3.5-turbo": [{"prompt_tokens": 0}]}}
    )()
    with dspy.context(lm=fake_lm):
        monkeypatch.setattr(dspy.settings, "usage_tracker", fake_tracker, raising=False)
        n = app._last_prompt_tokens()
    assert n > 0


def test_per_expert_independent(arc, monkeypatch):
    """Each expert checks its OWN window; one over-threshold scope compacts, an
    under-threshold sibling does not."""
    _patch_prompt_tokens(monkeypatch, prompt_tokens=900)
    _populate(arc, scope="agentA/hot")
    _populate(arc, scope="agentA/cold")
    agent = make_react_agent()
    # hot: window 1000 -> 0.90 fires
    with _full_plane_context(arc, session=SID, scope="agentA/hot", window=1000):
        agent._maybe_autocompact()
    # cold: window 100000 -> 0.009 does not fire
    with _full_plane_context(arc, session=SID, scope="agentA/cold", window=100000):
        agent._maybe_autocompact()
    assert arc.render_segments_keys(SID, "agentA/hot") == {"observation_0": "COMPACT_SUMMARY"}
    assert "O0" in str(arc.render_segments_keys(SID, "agentA/cold"))  # untouched
