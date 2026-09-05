"""P2.4 #1072 — BeforeModel / AfterModel via the per-request ``dspy.LM`` wrapper.

Exercises the real hook wire (the industry exit-0/exit-2 subprocess adapter, no
mocked transport) driving a custom ``dspy.LM`` wrapper injected the way clio injects
it — ``dspy.context(lm=HookedLM(real_lm))``. Proves the four contract invariants:

* **synthesize** truly skips the real LM (asserted via a spy LM that raises/counts);
* **routing** actually calls the swapped LM, not the default;
* **modify** feeds the real LM the patched request;
* **AfterModel** rewrites only what enters context;
* **per-request** granularity: N LM calls in one bound context fire BeforeModel N
  times (the whole reason it lives at the LM boundary, not a turn seam);
* **no-hook** is pure pass-through (the wrapper is never even constructed);
* **offline replay**: a recorded BeforeModel response + a recorded tool result
  replay deterministically with ZERO real LM/tool calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy
import pytest

from clio_agent.gact.hooks import (
    HookDispatcher,
    ModelRequest,
    dispatch_pre_tool,
    hook_reasons,
    install_global_dispatcher,
    intercept_from_outcome,
    parse_hook_entries,
    stash_pre_tool_intercept,
    take_pre_tool_intercept,
)
from clio_agent.gact.hooks.dispatcher import (
    dispatch_after_model,
    dispatch_before_model,
    model_hooks_active,
)
from clio_agent.lm.hooked_lm import (
    HookDeniedModelCall,
    build_hooked_lm,
    effective_lm_for_call,
    install_model_route_resolver,
    wrap_lm_with_hooks,
)
from tests.test_gact._hook_fixtures import command_run, write_hook_script


# --------------------------------------------------------------------------- #
# Spy LMs — real dspy.BaseLM subclasses. No mocking of the call boundary.       #
# --------------------------------------------------------------------------- #
def _fake_response(text: str) -> SimpleNamespace:
    """A minimal OpenAI-chat-shaped response ``BaseLM._process_completion`` accepts."""

    message = SimpleNamespace(content=text, reasoning_content=None, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop", logprobs=None)
    return SimpleNamespace(choices=[choice], usage={}, model="spy/model", _hidden_params={})


class SpyLM(dspy.BaseLM):
    """Counts calls and records the messages it received; returns a canned answer."""

    def __init__(self, model: str = "spy/model", answer: str = "REAL-LM-ANSWER") -> None:
        super().__init__(model=model)
        self.calls = 0
        self.last_messages: Any = None
        self._answer = answer

    def forward(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> Any:
        self.calls += 1
        self.last_messages = messages
        return _fake_response(self._answer)


class ExplodingLM(dspy.BaseLM):
    """Fails if ever called — proves synthesize never reaches the real LM."""

    def __init__(self) -> None:
        super().__init__(model="explode/model")

    def forward(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> Any:
        raise AssertionError("the real LM must NOT be called on a synthesize path")


@pytest.fixture(autouse=True)
def _clean_global_hooks() -> Any:
    """Every test starts and ends with no global dispatcher / route resolver."""

    install_global_dispatcher(None)
    install_model_route_resolver(None)
    yield
    install_global_dispatcher(None)
    install_model_route_resolver(None)


def _install(rows: list[dict[str, Any]]) -> HookDispatcher:
    disp = HookDispatcher(parse_hook_entries(rows, source="test"))
    install_global_dispatcher(disp)
    return disp


def _command_row(hook_id: str, event: str, script: Path) -> dict[str, Any]:
    return {"id": hook_id, "on": [event], "run": command_run(script)}


# --------------------------------------------------------------------------- #
# synthesize — the real LM is NEVER called.                                    #
# --------------------------------------------------------------------------- #
def test_before_model_synthesize_skips_real_lm(tmp_path: Path) -> None:
    script = write_hook_script(
        tmp_path,
        "synth.py",
        "import json,sys\n"
        "json.load(sys.stdin)\n"
        'print(json.dumps({"decision": "synthesize", "llm_response": ["CANNED-ANSWER"]}))\n',
    )
    _install([_command_row("bm-synth", "BeforeModel", script)])
    spy = ExplodingLM()
    wrapped = wrap_lm_with_hooks(spy)
    # wrapped is a real HookedLM (model hooks are active).
    assert type(wrapped).__name__ == "HookedLM"

    outputs = wrapped(messages=[{"role": "user", "content": "hi"}])

    assert outputs == ["CANNED-ANSWER"]  # canned response used, real LM never invoked


def test_before_model_synthesize_via_dspy_predict(tmp_path: Path) -> None:
    """The wrapper works end-to-end as ``dspy.settings.lm`` through the adapter."""

    script = write_hook_script(
        tmp_path,
        "synth_fmt.py",
        "import json,sys\n"
        "json.load(sys.stdin)\n"
        'print(json.dumps({"decision": "synthesize", "llm_response": '
        '["[[ ## answer ## ]]\\nHELLO-FROM-CACHE\\n\\n[[ ## completed ## ]]"]}))\n',
    )
    _install([_command_row("bm-synth-fmt", "BeforeModel", script)])
    spy = ExplodingLM()
    wrapped = wrap_lm_with_hooks(spy)
    with dspy.context(lm=wrapped, adapter=dspy.ChatAdapter()):
        prediction = dspy.Predict("question -> answer")(question="anything")
    assert prediction.answer == "HELLO-FROM-CACHE"


def test_synthesize_without_response_falls_through_to_real_lm(tmp_path: Path) -> None:
    """A synthesize with no ``llm_response`` cannot skip — real LM runs, typed reason."""

    script = write_hook_script(
        tmp_path,
        "synth_empty.py",
        "import json,sys\n"
        "json.load(sys.stdin)\n"
        'print(json.dumps({"decision": "synthesize"}))\n',  # no llm_response
    )
    _install([_command_row("bm-synth-empty", "BeforeModel", script)])
    spy = SpyLM(answer="REAL")
    wrapped = wrap_lm_with_hooks(spy)

    outputs = wrapped(messages=[{"role": "user", "content": "hi"}])

    assert outputs == ["REAL"]
    assert spy.calls == 1  # the real LM ran
    reasons = {r["reason"] for r in hook_reasons()}
    assert "hook_synthesize_missing_llm_response" in reasons


# --------------------------------------------------------------------------- #
# routing — the swapped LM is invoked, not the default.                        #
# --------------------------------------------------------------------------- #
def test_model_routing_invokes_routed_lm(tmp_path: Path) -> None:
    script = write_hook_script(
        tmp_path,
        "route.py",
        "import json,sys\n"
        "json.load(sys.stdin)\n"
        'print(json.dumps({"decision": "modify", "model_override": "cheap"}))\n',
    )
    _install([_command_row("bm-route", "BeforeModel", script)])
    default = SpyLM(model="default/model", answer="DEFAULT")
    cheap = SpyLM(model="cheap/model", answer="CHEAP")
    # Per-instance route resolver maps the override name to the alternate LM.
    wrapped = build_hooked_lm(default, route_resolver={"cheap": cheap}.get)

    outputs = wrapped(messages=[{"role": "user", "content": "hi"}])

    assert outputs == ["CHEAP"]
    assert cheap.calls == 1  # the routed LM ran
    assert default.calls == 0  # the default did NOT


def test_effective_target_is_per_wrapper_and_cleared_for_synthetic_or_failed_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair identity never reuses a real target from an earlier hook call."""
    from clio_agent.gact.hooks.wire import HookOutcome
    from clio_agent.lm import hooked_lm as hooked_mod

    default = SpyLM(model="default/model")
    routed = SpyLM(model="routed/model")
    wrapped = build_hooked_lm(default, route_resolver={"routed": routed}.get)
    monkeypatch.setattr(
        hooked_mod,
        "dispatch_before_model",
        lambda *args, **kwargs: HookOutcome(decision="modify", model_override="routed"),
    )
    monkeypatch.setattr(hooked_mod, "dispatch_after_model", lambda *a, **k: HookOutcome())
    wrapped(messages=[{"role": "user", "content": "route"}])
    assert effective_lm_for_call(wrapped) is routed

    monkeypatch.setattr(
        hooked_mod,
        "dispatch_before_model",
        lambda *args, **kwargs: HookOutcome(
            decision="synthesize", llm_response=["synthetic"], llm_response_present=True
        ),
    )
    sentinel = object()
    assert wrapped(messages=[{"role": "user", "content": "synth"}]) == ["synthetic"]
    assert effective_lm_for_call(wrapped, sentinel) is sentinel

    def fail_hook(*args: Any, **kwargs: Any) -> HookOutcome:
        raise RuntimeError("hook failed")

    monkeypatch.setattr(hooked_mod, "dispatch_before_model", fail_hook)
    with pytest.raises(RuntimeError, match="hook failed"):
        wrapped(messages=[{"role": "user", "content": "fail"}])
    assert effective_lm_for_call(wrapped, sentinel) is sentinel


def test_effective_routed_targets_are_concurrent_wrapper_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from clio_agent.gact.hooks.wire import HookOutcome
    from clio_agent.lm import hooked_lm as hooked_mod

    monkeypatch.setattr(
        hooked_mod,
        "dispatch_before_model",
        lambda request, **kwargs: HookOutcome(decision="modify", model_override=request.model),
    )
    monkeypatch.setattr(hooked_mod, "dispatch_after_model", lambda *a, **k: HookOutcome())
    targets = {f"default/{i}": SpyLM(model=f"target/{i}") for i in range(8)}
    wrappers = [build_hooked_lm(SpyLM(model=name), route_resolver=targets.get) for name in targets]

    def invoke(wrapper: Any) -> Any:
        wrapper(messages=[{"role": "user", "content": "go"}])
        return effective_lm_for_call(wrapper)

    with ThreadPoolExecutor(max_workers=8) as pool:
        actual = list(pool.map(invoke, wrappers))
    assert actual == list(targets.values())


def test_route_unresolved_falls_back_to_default(tmp_path: Path) -> None:
    """An unresolvable override runs the default and records a typed reason."""

    script = write_hook_script(
        tmp_path,
        "route_bad.py",
        "import json,sys\n"
        "json.load(sys.stdin)\n"
        'print(json.dumps({"decision": "modify", "model_override": "nonexistent"}))\n',
    )
    _install([_command_row("bm-route-bad", "BeforeModel", script)])
    default = SpyLM(answer="DEFAULT")
    wrapped = build_hooked_lm(default, route_resolver=lambda _name: None)

    outputs = wrapped(messages=[{"role": "user", "content": "hi"}])

    assert outputs == ["DEFAULT"]
    assert default.calls == 1
    reasons = {r["reason"] for r in hook_reasons()}
    assert "hook_route_unresolved" in reasons


# --------------------------------------------------------------------------- #
# modify — the real LM receives the patched request.                          #
# --------------------------------------------------------------------------- #
def test_modify_request_patch_reaches_real_lm(tmp_path: Path) -> None:
    script = write_hook_script(
        tmp_path,
        "redact.py",
        "import json,sys\n"
        "json.load(sys.stdin)\n"
        'print(json.dumps({"decision": "modify", "request_patch": '
        '{"messages": [{"role": "user", "content": "REDACTED"}]}}))\n',
    )
    _install([_command_row("bm-modify", "BeforeModel", script)])
    spy = SpyLM(answer="OK")
    wrapped = wrap_lm_with_hooks(spy)

    outputs = wrapped(messages=[{"role": "user", "content": "SECRET-TOKEN=abc123"}])

    assert outputs == ["OK"]
    assert spy.calls == 1
    # The real LM saw the PATCHED messages, never the original secret.
    assert spy.last_messages == [{"role": "user", "content": "REDACTED"}]


# --------------------------------------------------------------------------- #
# AfterModel — rewrites only what enters context.                             #
# --------------------------------------------------------------------------- #
def test_after_model_rewrites_response(tmp_path: Path) -> None:
    script = write_hook_script(
        tmp_path,
        "after.py",
        "import json,sys\n"
        "json.load(sys.stdin)\n"
        'print(json.dumps({"llm_response": ["SANITISED"]}))\n',
    )
    _install([_command_row("am-rewrite", "AfterModel", script)])
    spy = SpyLM(answer="RAW-MODEL-OUTPUT")
    wrapped = wrap_lm_with_hooks(spy)

    outputs = wrapped(messages=[{"role": "user", "content": "hi"}])

    assert spy.calls == 1  # the real call still ran (AfterModel cannot un-run it)
    assert outputs == ["SANITISED"]  # but what enters context is the rewrite


def test_after_model_sees_synthetic_flag(tmp_path: Path) -> None:
    """AfterModel fires on a synthesized response, flagged ``synthetic: true``."""

    before = write_hook_script(
        tmp_path,
        "b.py",
        "import json,sys\n"
        "json.load(sys.stdin)\n"
        'print(json.dumps({"decision": "synthesize", "llm_response": ["CANNED"]}))\n',
    )
    after = write_hook_script(
        tmp_path,
        "a.py",
        "import json,sys\n"
        "d = json.load(sys.stdin)\n"
        "syn = d.get('payload', {}).get('synthetic')\n"
        'print(json.dumps({"llm_response": ["SYNTHETIC" if syn else "NOT"]}))\n',
    )
    _install(
        [
            _command_row("bm", "BeforeModel", before),
            _command_row("am", "AfterModel", after),
        ]
    )
    spy = ExplodingLM()
    wrapped = wrap_lm_with_hooks(spy)

    outputs = wrapped(messages=[{"role": "user", "content": "hi"}])

    assert outputs == ["SYNTHETIC"]


# --------------------------------------------------------------------------- #
# deny — a BeforeModel hook blocks the model request (typed, never silent).    #
# --------------------------------------------------------------------------- #
def test_before_model_deny_blocks_the_call(tmp_path: Path) -> None:
    script = write_hook_script(
        tmp_path,
        "deny.py",
        "import sys\nprint('policy refused this request', file=sys.stderr)\nsys.exit(2)\n",
    )
    _install([_command_row("bm-deny", "BeforeModel", script)])
    spy = SpyLM()
    wrapped = wrap_lm_with_hooks(spy)

    with pytest.raises(HookDeniedModelCall) as excinfo:
        wrapped(messages=[{"role": "user", "content": "hi"}])

    assert spy.calls == 0  # the real LM never ran
    assert "policy refused" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# PER-REQUEST — N calls in one bound context fire BeforeModel N times.         #
# --------------------------------------------------------------------------- #
class _CountingDispatcher(HookDispatcher):
    """Counts dispatch() per event so the per-request invariant is provable."""

    def __init__(self, entries: Any) -> None:
        super().__init__(entries)
        self.counts: dict[str, int] = {}

    def dispatch(self, event: str, envelope: Any) -> Any:
        self.counts[event] = self.counts.get(event, 0) + 1
        return super().dispatch(event, envelope)


def test_per_request_fires_before_model_once_per_lm_call(tmp_path: Path) -> None:
    """Two LM calls under ONE bound wrapper fire BeforeModel twice (not once)."""

    script = write_hook_script(
        tmp_path,
        "noop.py",
        "import json,sys\njson.load(sys.stdin)\nprint(json.dumps({}))\n",  # allow
    )
    disp = _CountingDispatcher(
        parse_hook_entries([_command_row("bm", "BeforeModel", script)], source="t")
    )
    install_global_dispatcher(disp)
    spy = SpyLM(answer="X")
    wrapped = wrap_lm_with_hooks(spy)

    # A "turn" binds the wrapper once; the program then makes several model calls.
    with dspy.context(lm=wrapped):
        wrapped(messages=[{"role": "user", "content": "step-1"}])
        wrapped(messages=[{"role": "user", "content": "step-2"}])
        wrapped(messages=[{"role": "user", "content": "step-3"}])

    assert disp.counts.get("BeforeModel") == 3  # once PER request, not once per turn
    assert spy.calls == 3


def test_per_request_through_real_dspy_program(tmp_path: Path) -> None:
    """Two dspy.Predict calls on the bound wrapper each fire BeforeModel."""

    script = write_hook_script(
        tmp_path, "noop2.py", "import json,sys\njson.load(sys.stdin)\nprint(json.dumps({}))\n"
    )
    disp = _CountingDispatcher(
        parse_hook_entries([_command_row("bm", "BeforeModel", script)], source="t")
    )
    install_global_dispatcher(disp)
    spy = SpyLM(answer="[[ ## answer ## ]]\nA\n\n[[ ## completed ## ]]")
    wrapped = wrap_lm_with_hooks(spy)
    with dspy.context(lm=wrapped, adapter=dspy.ChatAdapter()):
        program = dspy.Predict("question -> answer")
        program(question="one")
        program(question="two")
    assert disp.counts.get("BeforeModel") == 2


# --------------------------------------------------------------------------- #
# no-hook — pure pass-through, zero wrapper.                                   #
# --------------------------------------------------------------------------- #
def test_no_model_hook_is_pure_passthrough() -> None:
    # No dispatcher / no BeforeModel-or-AfterModel entries installed.
    assert model_hooks_active() is False
    spy = SpyLM()
    result = wrap_lm_with_hooks(spy)
    assert result is spy  # the SAME object — never wrapped, zero overhead


def test_tool_only_dispatcher_does_not_wrap(tmp_path: Path) -> None:
    """A dispatcher with only tool hooks leaves the LM unwrapped (no model hooks)."""

    script = write_hook_script(
        tmp_path, "pt.py", "import json,sys\njson.load(sys.stdin)\nprint('{}')\n"
    )
    _install([_command_row("pt", "PreToolUse", script)])
    assert model_hooks_active() is False
    spy = SpyLM()
    assert wrap_lm_with_hooks(spy) is spy


# --------------------------------------------------------------------------- #
# OFFLINE REPLAY — BeforeModel synthesize + tool synthesize, zero real calls.  #
# --------------------------------------------------------------------------- #
def test_offline_replay_model_and_tool_zero_real_calls(tmp_path: Path) -> None:
    """A recorded model exchange + a recorded tool result replay deterministically.

    A recording file maps the last user message to a canned completion; a
    BeforeModel hook serves it (real LM never called). A PreToolUse synthesize hook
    serves a recorded tool result (the P2.3 seam). Two calls with different prompts
    replay their two distinct recorded answers — deterministic, zero real LM/tool.
    """

    recording = tmp_path / "recording.json"
    recording.write_text(
        json.dumps({"weather in LA?": ["It is sunny."], "weather in NY?": ["It is raining."]}),
        encoding="utf-8",
    )
    model_hook = write_hook_script(
        tmp_path,
        "replay_model.py",
        "import json,sys\n"
        "env = json.load(sys.stdin)\n"
        f"rec = json.load(open({str(recording)!r}, encoding='utf-8'))\n"
        "msgs = env.get('model_request', {}).get('messages', [])\n"
        "key = msgs[-1]['content'] if msgs else ''\n"
        "resp = rec.get(key)\n"
        "print(json.dumps({'decision': 'synthesize', 'llm_response': resp} if resp is not None else {}))\n",
    )
    tool_hook = write_hook_script(
        tmp_path,
        "replay_tool.py",
        "import json,sys\n"
        "json.load(sys.stdin)\n"
        'print(json.dumps({"decision": "synthesize", "result": "RECORDED-TOOL-RESULT"}))\n',
    )
    _install(
        [
            _command_row("replay-model", "BeforeModel", model_hook),
            _command_row("replay-tool", "PreToolUse", tool_hook),
        ]
    )
    spy = ExplodingLM()
    wrapped = wrap_lm_with_hooks(spy)

    # Model half: two distinct recorded exchanges, zero real LM calls.
    out_la = wrapped(messages=[{"role": "user", "content": "weather in LA?"}])
    out_ny = wrapped(messages=[{"role": "user", "content": "weather in NY?"}])
    assert out_la == ["It is sunny."]
    assert out_ny == ["It is raining."]

    # Determinism: re-running yields the identical recorded answers.
    assert wrapped(messages=[{"role": "user", "content": "weather in LA?"}]) == ["It is sunny."]

    # Tool half (the P2.3 synthesize seam): a recorded tool result, tool never runs.
    outcome = dispatch_pre_tool("get_weather", {"city": "LA"})
    stash_pre_tool_intercept(outcome)
    decision = take_pre_tool_intercept()
    assert intercept_from_outcome(outcome) is not None
    assert decision is not None
    assert decision.kind == "synthesize"
    assert decision.result == "RECORDED-TOOL-RESULT"


# --------------------------------------------------------------------------- #
# The public ModelRequest contract + dispatch helpers.                        #
# --------------------------------------------------------------------------- #
def test_model_request_is_versioned_and_minimal() -> None:
    req = ModelRequest(
        model="openai/gpt-x",
        messages=[{"role": "user", "content": "hi"}],
        params={"temperature": 0.0},
        tools=[{"name": "t"}],
    )
    body = req.to_json()
    assert body == {
        "model": "openai/gpt-x",
        "messages": [{"role": "user", "content": "hi"}],
        "params": {"temperature": 0.0},
        "tools": [{"name": "t"}],
    }


def test_dispatch_helpers_no_global_are_empty_allow() -> None:
    install_global_dispatcher(None)
    req = ModelRequest(model="m", messages=[])
    assert dispatch_before_model(req).decision == "allow"
    assert dispatch_after_model(req, response=["x"], synthetic=False).decision == "allow"


def test_credentials_are_never_in_the_model_request(tmp_path: Path) -> None:
    """api_key / api_base are stripped from the params a BeforeModel hook sees."""

    dump = tmp_path / "seen.json"
    script = write_hook_script(
        tmp_path,
        "capture.py",
        "import json,sys\n"
        "env = json.load(sys.stdin)\n"
        f"open({str(dump)!r}, 'w', encoding='utf-8').write(json.dumps(env['model_request']))\n"
        "print(json.dumps({}))\n",
    )
    _install([_command_row("capture", "BeforeModel", script)])
    spy = SpyLM()
    wrapped = wrap_lm_with_hooks(spy)
    wrapped(
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.2,
        api_key="SECRET",
        api_base="https://secret.example",
    )
    seen = json.loads(dump.read_text(encoding="utf-8"))
    assert seen["params"].get("temperature") == 0.2
    assert "api_key" not in seen["params"]
    assert "api_base" not in seen["params"]
    # But the real call still received the credentials (the wrapper never strips them
    # from the outgoing call — only from the hook's VIEW).
    assert spy.calls == 1
