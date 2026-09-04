"""S5 part 2 (#948 / #953): declarable ``dspy.BestOfN`` / ``dspy.Refine`` variants.

Four layers are pinned:

* **Declaration matrix** (``runtime.type_parsing.parse_module_variant`` + the
  ``expert_packs.parse_expert_file`` loader) — a valid ``best_of_n``/``refine`` parses;
  an unknown variant / bad ``n`` / missing reward each raise the SAME typed error the
  loader surfaces on the row.
* **Reward compilation** (``module_variants.compile_reward_fn``) — the generated reward
  is a source-backed real ``def`` (``inspect.getsource`` succeeds, so ``dspy.Refine``
  can adopt it), scores via a stubbed judge ``dspy.Predict``, and a parse failure scores
  ``0.0`` with a typed log (never a crash).
* **Wrap dispatch** (``module_variants.wrap_module_variant``) — a variant declaration
  builds a REAL ``dspy.BestOfN``/``Refine`` wrapping the declared inner kind, selects by
  the compiled reward, and stamps the winning try's index + score.
* **ARC-plane run keying** (``context.run_keyed_scope`` through the real
  ``reactv2.arc_history_messages`` fold + the ``lm_activity``/``tool_observer``
  attribution seams) — two in-process tries of one module in one session read DISTINCT
  ARC partitions (sabotage: drop the ``react_run`` fold → try 2's fold contains try 1's
  trajectory → red), while every attribution reader keeps the BARE agent id.
"""

from __future__ import annotations

import inspect
import types
from pathlib import Path
from typing import Any, Iterator

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context as ctx
from clio_agent.gact.agents import module_variants as mv
from clio_agent.gact.agents.reactv2 import arc_history_messages
from clio_agent.gact.app import _build_blueprint_dspy_module, build_app
from clio_agent.gact.expert_packs import parse_expert_file
from clio_agent.gact.runtime.type_parsing import (
    VariantSpec,
    _blueprint_module_variant,
    parse_module_variant,
)
from clio_agent.gact.types import AgentDef

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _reward_decl(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"instructions": "Score how well the answer resolves the question."}
    base.update(over)
    return base


def _module(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "predict",
        "variant": "best_of_n",
        "n": 3,
        "threshold": 0.8,
        "reward": _reward_decl(),
    }
    base.update(over)
    return base


def _agent(module: dict[str, Any], *, agent_id: str = "judge") -> types.SimpleNamespace:
    return types.SimpleNamespace(id=agent_id, module=module)


# --------------------------------------------------------------------------- #
# 1. declaration matrix
# --------------------------------------------------------------------------- #


def test_absent_variant_parses_to_none() -> None:
    assert parse_module_variant({"kind": "react"}, agent_id="x") is None
    assert parse_module_variant({"kind": "react", "variant": ""}, agent_id="x") is None


def test_valid_best_of_n_declaration_parses() -> None:
    spec = parse_module_variant(
        _module(variant="best_of_n", n=3, threshold=0.8, reward=_reward_decl(inputs=["question"])),
        agent_id="judge",
    )
    assert isinstance(spec, VariantSpec)
    assert (spec.variant, spec.n, spec.threshold) == ("best_of_n", 3, 0.8)
    assert spec.reward_inputs == ("question",)
    assert spec.reward_target == "answer"  # default target
    assert spec.reward_instructions.startswith("Score")


def test_valid_refine_declaration_parses_with_mapping_inputs_and_target() -> None:
    spec = parse_module_variant(
        {
            "kind": "react",
            "variant": "refine",
            "n": 2,
            # threshold omitted -> parser default 1.0
            "reward": _reward_decl(
                inputs={"question": "the user ask", "region": "the AOI"},
                target="answer",
            ),
        },
        agent_id="orch",
    )
    assert spec is not None
    assert spec.variant == "refine"
    assert spec.threshold == 1.0  # default threshold
    assert spec.reward_inputs == ("question", "region")
    assert spec.reward_input_descs == {"question": "the user ask", "region": "the AOI"}


def test_refine_composes_with_react_inner_no_validation_error() -> None:
    """Verified against the installed dspy/predict/refine.py: Refine is module-agnostic
    (drives the inner via named_predictors + inspect.getsource on the wrapper class), so
    refine + react is ALLOWED, not a typed error."""
    spec = parse_module_variant(_module(kind="react", variant="refine"), agent_id="orch")
    assert spec is not None and spec.variant == "refine"


@pytest.mark.parametrize(
    ("module", "needle"),
    [
        (_module(variant="beam_search"), "unsupported module.variant"),
        (_module(n=0), "requires n >= 1"),
        (_module(n=-4), "requires n >= 1"),
        (
            {"kind": "predict", "variant": "best_of_n", "n": "three", "reward": _reward_decl()},
            "requires an integer n >= 1",
        ),
        ({"kind": "predict", "variant": "best_of_n", "n": 3}, "requires a reward declaration"),
        (_module(reward={"inputs": ["q"]}), "reward requires non-empty 'instructions'"),
        (_module(reward=_reward_decl(inputs="question")), "reward.inputs must be a list"),
        (_module(threshold="high"), "malformed threshold"),
    ],
)
def test_invalid_declarations_raise_typed_errors(module: dict[str, Any], needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        parse_module_variant(module, agent_id="judge")


def test_blueprint_module_variant_agentdef_adapter_matches_parser() -> None:
    agent = _agent(_module())
    spec = _blueprint_module_variant(agent)
    assert spec is not None and spec.variant == "best_of_n"
    # unset -> None
    assert _blueprint_module_variant(_agent({"kind": "predict"})) is None


# --------------------------------------------------------------------------- #
# 2. loader validation surfaces the same typed error on the row
# --------------------------------------------------------------------------- #


def _write_expert(tmp_path: Path, frontmatter: str) -> Path:
    path = tmp_path / "judge.md"
    path.write_text(f"---\n{frontmatter}\n---\nDo the work.\n", encoding="utf-8")
    return path


def test_loader_accepts_valid_variant(tmp_path: Path) -> None:
    row = parse_expert_file(
        _write_expert(
            tmp_path,
            "id: judge\n"
            "tier: 1\n"
            "module:\n"
            "  kind: predict\n"
            "  variant: best_of_n\n"
            "  n: 3\n"
            "  threshold: 0.8\n"
            "  reward:\n"
            "    instructions: Score the answer.\n"
            "    inputs:\n"
            "      - question\n",
        ),
        scope="workspace",
    )
    assert row.enabled is True
    assert not [e for e in row.validation_errors if "variant" in e]
    assert row.module.get("variant") == "best_of_n"


def test_loader_surfaces_typed_variant_error(tmp_path: Path) -> None:
    row = parse_expert_file(
        _write_expert(
            tmp_path,
            "id: judge\n"
            "module:\n"
            "  kind: predict\n"
            "  variant: best_of_n\n"
            "  n: 0\n"
            "  reward:\n"
            "    instructions: Score it.\n",
        ),
        scope="workspace",
    )
    assert row.enabled is False
    assert any("requires n >= 1" in e for e in row.validation_errors)


def test_loader_missing_reward_disables_row(tmp_path: Path) -> None:
    row = parse_expert_file(
        _write_expert(
            tmp_path,
            "id: judge\nmodule:\n  kind: predict\n  variant: refine\n  n: 2\n",
        ),
        scope="workspace",
    )
    assert row.enabled is False
    assert any("requires a reward declaration" in e for e in row.validation_errors)


# --------------------------------------------------------------------------- #
# 3. reward compilation
# --------------------------------------------------------------------------- #


def test_compiled_reward_is_source_backed_for_refine() -> None:
    """dspy.Refine calls inspect.getsource on the reward fn; the generated def must be
    source-backed."""
    spec = parse_module_variant(_module(), agent_id="judge")
    assert spec is not None
    reward_fn = mv.compile_reward_fn(spec, agent_id="judge")
    src = inspect.getsource(reward_fn)
    assert "def scored_reward" in src
    # a real dspy.Refine can adopt it without raising (it getsource's the reward fn).
    refine = dspy.Refine(
        module=dspy.Predict("question -> answer"), N=2, reward_fn=reward_fn, threshold=1.0
    )
    assert isinstance(refine, dspy.Refine)


def test_compiled_reward_scores_via_stubbed_judge() -> None:
    spec = parse_module_variant(
        _module(reward=_reward_decl(inputs=["question"], target="answer")), agent_id="judge"
    )
    assert spec is not None
    reward_fn = mv.compile_reward_fn(spec, agent_id="judge")
    with dspy.context(lm=DummyLM([{"score": "0.75"}])):
        score = reward_fn({"question": "capital of Belgium?"}, dspy.Prediction(answer="Brussels"))
    assert score == pytest.approx(0.75)


def test_compiled_reward_clamps_out_of_range() -> None:
    spec = parse_module_variant(_module(), agent_id="judge")
    assert spec is not None
    reward_fn = mv.compile_reward_fn(spec, agent_id="judge")
    with dspy.context(lm=DummyLM([{"score": "1.9"}, {"score": "-0.4"}])):
        hi = reward_fn({}, dspy.Prediction(answer="a"))
        lo = reward_fn({}, dspy.Prediction(answer="b"))
    assert (hi, lo) == (1.0, 0.0)


def test_compiled_reward_parse_failure_scores_zero_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = parse_module_variant(_module(), agent_id="judge")
    assert spec is not None
    reward_fn = mv.compile_reward_fn(spec, agent_id="judge")
    with caplog.at_level("WARNING", logger="clio_agent.gact.agents.module_variants"):
        with dspy.context(lm=DummyLM([{"score": "not-a-number"}])):
            score = reward_fn({}, dspy.Prediction(answer="a"))
    assert score == 0.0
    assert any("variant.reward.parse_failed" in rec.message for rec in caplog.records)


def test_clamp_score_raises_on_non_numeric() -> None:
    with pytest.raises((ValueError, TypeError)):
        mv._clamp_score("abc")


# --------------------------------------------------------------------------- #
# 4. wrap dispatch — real dspy.BestOfN / Refine over the declared kind
# --------------------------------------------------------------------------- #


def test_no_variant_returns_inner_unchanged() -> None:
    inner = dspy.Predict("question -> answer")
    assert mv.wrap_module_variant(inner, _agent({"kind": "predict"})) is inner


@pytest.mark.parametrize(
    ("kind", "inner_cls"),
    [("predict", dspy.Predict), ("chain_of_thought", dspy.ChainOfThought)],
)
def test_wrap_builds_real_bestofn_over_declared_kind(kind: str, inner_cls: type) -> None:
    inner = inner_cls("question -> answer")
    wrapped = mv.wrap_module_variant(inner, _agent(_module(kind=kind, variant="best_of_n")))
    assert isinstance(wrapped, dspy.BestOfN)
    # the run-keyed wrapper interposes, and its .inner is the declared kind.
    assert isinstance(wrapped.module, mv._RunKeyedModule)
    assert wrapped.module.inner is inner
    assert wrapped.N == 3


def test_wrap_builds_real_refine() -> None:
    inner = dspy.Predict("question -> answer")
    wrapped = mv.wrap_module_variant(inner, _agent(_module(variant="refine", n=2)))
    assert isinstance(wrapped, dspy.Refine)
    assert wrapped.module.inner is inner


def test_wrapped_variant_runs_and_selects_by_reward() -> None:
    """End-to-end: the real BestOfN loop runs N tries, the compiled reward selects the
    best, and the winning index + score are stamped on the prediction."""
    inner = dspy.Predict("question -> answer")
    wrapped = mv.wrap_module_variant(inner, _agent(_module(n=2, threshold=1.0)))
    # DummyLM cycles: try0 answer, try0 judge, try1 answer, try1 judge.
    lm = DummyLM(
        [
            {"answer": "wordy first attempt"},
            {"score": "0.3"},
            {"answer": "second"},
            {"score": "0.9"},
        ]
    )
    with dspy.context(lm=lm):
        pred = wrapped(question="pick the best")
    assert pred is not None
    # #953 [9]: the RETURNED prediction is the WINNING try's output, not just a stamp
    # computed independently from the ledger — a regression returning the losing try's
    # prediction ("wordy first attempt") goes red here.
    assert pred.answer == "second"
    sel = pred.variant_selection
    assert sel["variant"] == "best_of_n"
    assert sel["n"] == 2
    assert sel["winning_index"] == 1
    assert sel["winning_score"] == pytest.approx(0.9)
    assert {s["run_index"] for s in sel["scores"]} == {0, 1}


# --------------------------------------------------------------------------- #
# 4b. total variant failure — ALWAYS a typed error, never N-dependent (#953 [4])
# --------------------------------------------------------------------------- #


class _BoomInner(dspy.Module):
    """A real inner dspy.Module whose forward raises on EVERY try (401/tool-crash/OOM
    stand-in), so the whole variant is a total failure."""

    def __init__(self, err: str = "boom-401") -> None:
        super().__init__()
        self.predict = dspy.Predict("question -> answer")
        self._err = err

    def forward(self, **_kwargs: Any) -> Any:
        raise RuntimeError(self._err)


@pytest.mark.parametrize("n", [1, 2, 3])
def test_total_variant_failure_raises_typed_for_every_n(n: int) -> None:
    """Every declared n (1, 2, 3) that totally fails raises the SAME typed error carrying
    the real root cause. Sabotage/regression: the pre-fix wrapper returned None for n<=2
    (dspy fail_count off-by-one) → no raise → red here; n>=3 raised a bare RuntimeError."""
    wrapped = mv.wrap_module_variant(_BoomInner(), _agent(_module(n=n, threshold=1.0)))
    with dspy.context(lm=DummyLM([{"answer": "x"}])):
        with pytest.raises(mv.VariantTotalFailure) as excinfo:
            wrapped(question="q")
    err = excinfo.value
    assert err.variant == "best_of_n"
    assert err.last_error == "RuntimeError: boom-401"
    assert len(err.per_try_errors) == n  # every try's real error captured, not swallowed
    assert "exhausted all" in str(err)


def test_total_variant_failure_outcome_is_identical_across_n() -> None:
    """The engine is N-dependent on total failure (None for n<=2, raise for n>=3); the
    wrapper normalizes BOTH into one typed outcome — identical type + last error for n=1,2,3."""
    outcomes: dict[int, mv.VariantTotalFailure] = {}
    for n in (1, 2, 3):
        wrapped = mv.wrap_module_variant(_BoomInner(), _agent(_module(n=n, threshold=1.0)))
        with dspy.context(lm=DummyLM([{"answer": "x"}])):
            with pytest.raises(mv.VariantTotalFailure) as excinfo:
                wrapped(question="q")
        outcomes[n] = excinfo.value
    assert {o.last_error for o in outcomes.values()} == {"RuntimeError: boom-401"}
    assert [len(outcomes[n].per_try_errors) for n in (1, 2, 3)] == [1, 2, 3]


def test_total_refine_failure_is_typed_too() -> None:
    """Refine shares the same fail_count guard; total failure is likewise typed."""
    wrapped = mv.wrap_module_variant(_BoomInner(), _agent(_module(variant="refine", n=2)))
    with dspy.context(lm=DummyLM([{"answer": "x"}])):
        with pytest.raises(mv.VariantTotalFailure) as excinfo:
            wrapped(question="q")
    assert excinfo.value.variant == "refine"
    assert excinfo.value.last_error == "RuntimeError: boom-401"


# --------------------------------------------------------------------------- #
# 4d. a deterministic MCP protocol refusal is NEVER retried (#1282 F7)         #
# --------------------------------------------------------------------------- #


def _make_refusing_inner() -> tuple[dspy.Module, list[int]]:
    """A real inner dspy.Module whose forward raises a TYPED, deterministic MCP
    protocol refusal on every call. Call count lives in a LIST captured by the
    ``forward`` closure -- NEVER on ``self`` -- because ``dspy.BestOfN``/
    ``Refine`` run each try against ``self.module.deepcopy()``; an instance
    attribute would be cloned per try (undercounting to a permanent 0), while
    a closure-captured list survives untouched (``copy.deepcopy`` treats a
    function/its closure cells as atomic, never recursing into them), so every
    deepcopy's call still increments the ONE shared counter. Carries a real
    ``dspy.Predict`` submodule (unused, never reached) so ``get_lm()``/
    ``deepcopy()`` behave exactly like ``_BoomInner`` above."""
    from clio_agent.errors import MCPMissingRequiredClientCapabilityError

    calls: list[int] = []

    class _RefusingInner(dspy.Module):
        def __init__(self) -> None:
            super().__init__()
            self.predict = dspy.Predict("question -> answer")

        def forward(self, **_kwargs: Any) -> Any:
            calls.append(1)
            raise MCPMissingRequiredClientCapabilityError(
                "task_echo requires the tasks extension",
                {"requiredCapabilities": {"extensions": {"io.modelcontextprotocol/tasks": {}}}},
            )

    return _RefusingInner(), calls


@pytest.mark.parametrize(
    "variant_kwargs", [{"n": 3, "threshold": 1.0}, {"variant": "refine", "n": 3}]
)
def test_deterministic_refusal_is_never_retried(variant_kwargs: dict[str, Any]) -> None:
    """#1282 F7: a deterministic MCP protocol refusal escalates AS ITSELF (never
    wrapped in ``VariantTotalFailure``, which would lose the typed reason/
    protocol_data D1/D2 carry), and the inner module runs exactly ONCE across
    ``N`` declared tries -- BestOfN/Refine's own ``except Exception`` retry
    loop cannot tell a refusal from an ordinary failure, so CLIO's own
    per-try wrapper (module_variants._RunKeyedModule) short-circuits every
    try after the first instead of re-invoking a call that can never
    succeed."""
    from clio_agent.errors import MCPMissingRequiredClientCapabilityError

    inner, calls = _make_refusing_inner()
    wrapped = mv.wrap_module_variant(inner, _agent(_module(**variant_kwargs)))
    with dspy.context(lm=DummyLM([{"answer": "x"}])):
        with pytest.raises(MCPMissingRequiredClientCapabilityError) as excinfo:
            wrapped(question="q")
    assert len(calls) == 1, "the inner module must run exactly once, never retried"
    assert "io.modelcontextprotocol/tasks" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 4c. builder integration lock — the wrap call at builders.py is exercised (#953 [10])
# --------------------------------------------------------------------------- #


def _resolved_spec_stub() -> Any:
    return types.SimpleNamespace(
        materialize=lambda cred=None: types.SimpleNamespace(
            provider="argonne", model="gpt-oss-120b", temperature=0.0
        )
    )


def _build_variant_module(
    monkeypatch: pytest.MonkeyPatch,
    module_decl: dict[str, Any],
    *,
    agent_id: str = "judge",
    lm: Any = None,
) -> Any:
    """Build a REAL BlueprintExpertModule through the production builder (the ONLY
    integration path that runs the ``self.program = _wrap_module_variant(...)`` seam)."""
    monkeypatch.setattr(
        "clio_agent.config.create_lm", lambda config: lm or DummyLM([{"answer": "x"}])
    )
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda config: dspy.ChatAdapter())
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._dynamic_agent_lm_config",
        lambda base_agent, agent_def: _resolved_spec_stub(),
    )
    return _build_blueprint_dspy_module(
        types.SimpleNamespace(),
        AgentDef(
            id=agent_id,
            source="expert_pack",
            title=agent_id,
            module=module_decl,
            # keep the runtime signature to just `answer` so a DummyLM forward is simple.
            structured_outputs={"workflow_state": False},
        ),
    )


@pytest.mark.parametrize("kind", ["predict", "chain_of_thought", "react"])
def test_builder_wires_variant_over_declared_kind(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """The BUILT program is the real dspy engine wrapping the declared inner kind. Sabotage:
    delete ``self.program = _wrap_module_variant(...)`` at builders.py → module.program is the
    bare inner (not BestOfN) → red."""
    module = _build_variant_module(monkeypatch, _module(kind=kind, variant="best_of_n", n=2))
    assert isinstance(module.program, dspy.BestOfN)
    assert isinstance(module.program.module, mv._RunKeyedModule)
    if kind == "react":
        # the react program tag survives the wrap (inner is the real retaining-react cls).
        assert getattr(module.program.module.inner, "_clio_expert_id", None) == "judge"


def test_variant_selection_reaches_assistant_message_metadata(
    tmp_path: Path, host_agent_executor: Any
) -> None:
    """A full turn whose prediction carries ``variant_selection`` surfaces it on the durable
    assistant message metadata (#953 [5]). Sabotage: without the turn_finalize carry (or the
    builders boundary carry) the field never reaches the persisted metadata → red."""
    from fastapi.testclient import TestClient

    from tests.test_gact.conftest import complete_turn

    sel = {
        "variant": "best_of_n",
        "n": 2,
        "winning_index": 1,
        "winning_score": 0.9,
        "scores": [{"run_index": 0, "score": 0.3}, {"run_index": 1, "score": 0.9}],
    }

    class _Host:
        def forward(self, question: str, session_id: str, **_kw: Any) -> Any:
            return type(
                "P",
                (),
                {
                    "answer": "done",
                    "selected_expert": "",
                    "routing_rationale": "",
                    "variant_selection": sel,
                },
            )()

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Host())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        asst = complete_turn(client, sid, "go")
    assert asst["metadata"]["variant_selection"] == sel


def test_builder_variant_forward_returns_winner_and_stamps_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forward the BUILT module under a stubbed LM: the returned prediction is the WINNING
    try's output AND carries variant_selection across the boundary (#953 [5]/[9]/[10]).
    Sabotage: delete the wrap call → a single-shot Predict returns 'wordy first attempt' with
    no variant_selection → both asserts red."""
    lm = DummyLM(
        [
            {"answer": "wordy first attempt"},
            {"score": "0.3"},
            {"answer": "second"},
            {"score": "0.9"},
        ]
    )
    module = _build_variant_module(
        monkeypatch,
        _module(
            kind="predict",
            variant="best_of_n",
            n=2,
            threshold=1.0,
            reward=_reward_decl(inputs=["question"]),
        ),
        lm=lm,
    )
    pred = module(question="pick the best", session_id="s1")
    assert pred.answer == "second"
    assert pred.variant_selection is not None
    assert pred.variant_selection["winning_index"] == 1
    assert pred.variant_selection["winning_score"] == pytest.approx(0.9)


# --------------------------------------------------------------------------- #
# 5. ARC-plane run keying isolation (real fold) + attribution unchanged
# --------------------------------------------------------------------------- #

_SESSION, _SCOPE = "sid-ensemble", "geospatial"


@pytest.fixture
def arc(tmp_path: Path) -> ARCMemory:
    return ARCMemory(data_dir=str(tmp_path / "arc"))


def _variant_scope_ctx(arc_memory: ARCMemory) -> Iterator[None]:
    app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc_memory))
    tokens = [
        ctx.set_app(app),
        ctx.set_react_scope(_SCOPE),
        ctx.set_react_session(_SESSION),
    ]
    try:
        yield
    finally:
        for tok in reversed(tokens):
            ctx.reset(tok)


def _write_thought(arc_memory: ARCMemory, text: str) -> None:
    """Write a thought through the SAME run-keyed scope the writer (reactv2_events
    _arc_scope) would compute for the active try."""
    scope = ctx.run_keyed_scope(ctx.active_react_scope())
    arc_memory.append_segment(
        _SESSION,
        scope,
        "thought",
        {"text": text},
        step=0,
        token_count=1,
    )


def test_sequential_tries_read_distinct_arc_partitions(arc: ARCMemory) -> None:
    """Two in-process tries of one module in one session fold DISTINCT ARC partitions —
    try 1's History fold never contains try 0's trajectory."""
    gen = _variant_scope_ctx(arc)
    next(gen)
    try:
        # try 0 writes its trajectory
        t0 = ctx.set_react_run(0)
        _write_thought(arc, "TRY0-THOUGHT")
        # its OWN fold sees it (partition is real, not always-empty)
        assert [m.get("next_thought") for m in (arc_history_messages() or [])] == ["TRY0-THOUGHT"]
        ctx.reset(t0)

        # try 1 folds its own (empty) partition — clean, no try-0 bleed
        t1 = ctx.set_react_run(1)
        try1_fold = arc_history_messages() or []
        assert try1_fold == []
        assert all("TRY0-THOUGHT" != m.get("next_thought") for m in try1_fold)
        ctx.reset(t1)
    finally:
        next(gen, None)


def test_sabotage_dropping_run_fold_leaks_prior_try(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sabotage the lock: if ``run_keyed_scope`` stops folding ``react_run`` (identity),
    try 1's fold accumulates try 0's trajectory — the exact model-input correctness bug
    the discriminator prevents. Proves the fold is load-bearing."""
    monkeypatch.setattr(ctx, "run_keyed_scope", lambda scope: scope)
    gen = _variant_scope_ctx(arc)
    next(gen)
    try:
        t0 = ctx.set_react_run(0)
        _write_thought(arc, "TRY0-THOUGHT")
        ctx.reset(t0)

        t1 = ctx.set_react_run(1)
        leaked = [m.get("next_thought") for m in (arc_history_messages() or [])]
        ctx.reset(t1)
    finally:
        next(gen, None)
    assert leaked == ["TRY0-THOUGHT"]  # RED without the real fold: try 1 saw try 0


def test_attribution_reads_bare_scope_under_variant_run() -> None:
    """The keying discriminator NEVER touches attribution: active_react_scope() (the
    id lm_activity agent_id / tool_observer invoking_expert read) stays bare while the
    KEYING helper folds the run index."""
    scope_tok = ctx.set_react_scope(_SCOPE)
    run_tok = ctx.set_react_run(2)
    try:
        assert ctx.active_react_scope() == _SCOPE  # attribution: bare
        assert ctx.run_keyed_scope(_SCOPE) == f"{_SCOPE}#run2"  # keying: partitioned
        assert ctx.active_react_run() == 2
    finally:
        ctx.reset(run_tok)
        ctx.reset(scope_tok)
    # off-variant the helper is a no-op (identity)
    assert ctx.run_keyed_scope(_SCOPE) == _SCOPE


# --------------------------------------------------------------------------- #
# 5b. run keying driven through a REAL variant forward (the production seam, #953 [11])
# --------------------------------------------------------------------------- #

_ARC_OBS: list[tuple[int, list[Any]]] = []


class _ArcWritingInner(dspy.Module):
    """A real inner that, per forward, records what its OWN run-keyed ARC partition folds
    BEFORE writing its thought, then writes it — through the SAME contextvar seams the
    production react writer uses (``active_app().state.arc`` + ``run_keyed_scope``), reached
    via contextvars so ``dspy.BestOfN``'s per-try ``module.deepcopy`` never clones them."""

    def __init__(self) -> None:
        super().__init__()
        self.predict = dspy.Predict("question -> answer")

    def forward(self, **kwargs: Any) -> Any:
        run = ctx.active_react_run()
        _ARC_OBS.append((run, [m.get("next_thought") for m in (arc_history_messages() or [])]))
        arc_memory = ctx.active_app().state.arc
        scope = ctx.run_keyed_scope(ctx.active_react_scope())
        arc_memory.append_segment(
            ctx.active_react_session(),
            scope,
            "thought",
            {"text": f"TRY{run}-THOUGHT"},
            step=0,
            token_count=1,
        )
        return self.predict(**kwargs)


def test_variant_forward_run_keys_arc_partitions_through_real_bestofn(arc: ARCMemory) -> None:
    """Drive the BUILT variant program through the REAL dspy.BestOfN loop over an ARC-writing
    inner: each try's ``react_run`` is set by the production ``_RunKeyedModule.forward`` (not
    hand-set), so try 1's History fold EXCLUDES try 0's trajectory and the two writes land in
    DISTINCT ARC partitions. This is the integration seam the piecewise tests above do not
    exercise (they set ``set_react_run`` manually). A full react inner is too heavy to drive
    deterministically under a stub LM, so this drives the minimal real path — the real BestOfN
    loop + real _RunKeyedModule + real reactv2 fold + real ARCMemory."""
    _ARC_OBS.clear()
    wrapped = mv.wrap_module_variant(
        _ArcWritingInner(),
        _agent(_module(n=2, threshold=1.0, reward=_reward_decl(inputs=["question"]))),
    )
    gen = _variant_scope_ctx(arc)
    next(gen)
    try:
        lm = DummyLM([{"answer": "a0"}, {"score": "0.1"}, {"answer": "a1"}, {"score": "0.2"}])
        with dspy.context(lm=lm):
            wrapped(question="q")
        # Each try, driven by the real loop, observed ONLY its own (empty) partition.
        assert _ARC_OBS == [(0, []), (1, [])]
        # ...and the two writes are in DISTINCT run-keyed partitions.
        t0 = ctx.set_react_run(0)
        fold0 = [m.get("next_thought") for m in (arc_history_messages() or [])]
        ctx.reset(t0)
        t1 = ctx.set_react_run(1)
        fold1 = [m.get("next_thought") for m in (arc_history_messages() or [])]
        ctx.reset(t1)
        assert fold0 == ["TRY0-THOUGHT"]
        assert fold1 == ["TRY1-THOUGHT"]
    finally:
        next(gen, None)


def test_sabotage_neutralizing_set_react_run_leaks_prior_try_in_real_forward(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sabotage the single line wiring the variant loop to the keying plane: force
    ``set_react_run`` to always bind run 0. Both tries then key to the same partition, so the
    second try's real-loop fold accumulates try 0's trajectory — the silent cross-try model
    -input contamination the discriminator prevents. Proves _RunKeyedModule's set_react_run is
    load-bearing through the real forward."""
    _ARC_OBS.clear()
    orig_set = ctx.set_react_run
    monkeypatch.setattr(ctx, "set_react_run", lambda _idx: orig_set(0))
    wrapped = mv.wrap_module_variant(
        _ArcWritingInner(),
        _agent(_module(n=2, threshold=1.0, reward=_reward_decl(inputs=["question"]))),
    )
    gen = _variant_scope_ctx(arc)
    next(gen)
    try:
        lm = DummyLM([{"answer": "a0"}, {"score": "0.1"}, {"answer": "a1"}, {"score": "0.2"}])
        with dspy.context(lm=lm):
            wrapped(question="q")
    finally:
        next(gen, None)
    # The SECOND forward saw the FIRST try's trajectory (leak) — red under the real fix.
    assert _ARC_OBS[1][1] == ["TRY0-THOUGHT"]
