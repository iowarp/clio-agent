"""Shared turn-execution harness for the GACT test suite (#948 S4b).

Background
----------
Before S4b the GACT turn engine had a fall-through ``else`` branch: a
default/``main`` session with no resolvable Agent Blueprint ran
``app.state.agent.forward(question, session_id)`` directly — the legacy Tier-1
``ClioAgent`` planner. Dozens of turn-engine tests exploited that seam by handing
``build_app(agent=<fake with a .forward>)`` a canned-``Prediction`` fake and
asserting the turn produced that prediction.

S4b deleted the legacy planner and its ``else`` branch. Every default/``main``
session now resolves a react ``main``, builds it through
``_build_blueprint_dspy_module(app.state.agent, dynamic_agent)`` and runs that
module ONCE in the turn's forward executor
(:func:`clio_agent.gact.turn_forward._run_module`). Which ``main`` -- a session
with an EXPLICITLY activated Agent Blueprint runs that blueprint's root; a BARE
session (no activation, owner ruling 2026-08-05, commit aa906022) runs the
in-code ``catalog._builtin_main_agent()`` instead, on the SAME builder seam
(``_agent_definition_uses_blueprint_runtime`` routes ``definition_kind:
builtin_main`` through it too). Either way that module is a real DSPy react
program that would call an LM — which unit tests do not have.

The seam
--------
:func:`install_host_agent_executor` monkeypatches the ONE builder seam the turn
engine resolves at call time (``clio_agent.gact.app._build_blueprint_dspy_module``,
re-imported inside ``forward_turn`` on every turn) so the "built module" simply
DELEGATES to the host agent's own ``forward(...)``. The fake host agent stays the
executor, so a test still expresses "the turn produces prediction X" by handing
``build_app`` a fake whose ``forward`` returns X — no per-test rewrite of the fake.

The turn calls the built module with the standard forward kwargs (``question``,
``session_id``, ``session_mode``, ``session_edit_mode``, ``cancel_requested`` and,
when the module's ``forward`` declares them, the native ``images``/``files``).
The delegating module accepts all of them and hands the host fake only the ones
its ``forward`` declares, so a legacy two-argument fake keeps working. The
delegating module declares ``images``/``files`` exactly when the host does, so
the turn's native-input gate sees the host's real capability.

Live text
---------
Live text reaches the transcript through the LM token hooks
(:mod:`clio_agent.runtime.lm_activity`), exactly as a real provider call's
streamed tokens do. :func:`emit_live_text` drives that hook from inside a fake
forward (it runs in the turn's executor with the turn context copied in), and
:func:`install_scripted_module` makes the turn build a module whose forward is a
test-supplied script.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

#: The kwargs the turn always passes to the built module (``turn_forward._run_module``).
_STANDARD_FORWARD_KWARGS = ("session_mode", "session_edit_mode", "cancel_requested")
#: The native model-input kwargs, passed only to a forward that declares them.
_NATIVE_INPUT_KWARGS = ("images", "files")


def _forward_parameters(forward: Any) -> tuple[set[str], bool]:
    """Return (declared parameter names, accepts ``**kwargs``) for ``forward``."""

    try:
        params = inspect.signature(forward).parameters
    except (TypeError, ValueError):
        return set(), False
    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    return set(params), accepts_var_kw


def _host_forward(base_agent: Any) -> Any:
    """The host fake's ``forward``, resolved without dspy's stack-walking getattr."""

    try:
        forward = inspect.getattr_static(base_agent, "forward")
    except AttributeError:
        return base_agent.forward
    if isinstance(forward, (staticmethod, classmethod)):
        return base_agent.forward
    if inspect.isfunction(forward):
        return forward.__get__(base_agent, type(base_agent))
    return base_agent.forward


def _is_dspy_module(agent: Any) -> bool:
    import dspy

    return isinstance(agent, dspy.Module)


class _HostAgentBlueprintModule:
    """A stand-in for the compiled blueprint DSPy module that delegates to the
    host agent's ``forward``.

    Accepts every standard forward kwarg the turn passes and forwards to the host
    only those its ``forward`` declares. Declares no native-input parameter, so a
    host whose ``forward`` takes no ``images`` is treated as such by the turn.
    """

    def __init__(self, base_agent: Any, agent_def: Any) -> None:
        self._base_agent = base_agent
        self.agent_def = agent_def

    def __call__(self, question: str, session_id: str, **kwargs: Any) -> Any:
        """Run the host fake as the turn runs a built module."""

        return self._delegate(question, session_id, kwargs)

    def forward(
        self,
        question: str,
        session_id: str,
        session_mode: str = "chat",
        session_edit_mode: str = "diff",
        cancel_requested: Any | None = None,
    ) -> Any:
        """Delegate to the host fake's ``forward``."""

        return self._delegate(
            question,
            session_id,
            {
                "session_mode": session_mode,
                "session_edit_mode": session_edit_mode,
                "cancel_requested": cancel_requested,
            },
        )

    def _delegate(self, question: str, session_id: str, kwargs: dict[str, Any]) -> Any:
        params, accepts_var_kw = _forward_parameters(_host_forward(self._base_agent))
        call_kwargs: dict[str, Any] = {"question": question, "session_id": session_id}
        defaults = {"session_mode": "chat", "session_edit_mode": "diff", "cancel_requested": None}
        for name in (*_STANDARD_FORWARD_KWARGS, *_NATIVE_INPUT_KWARGS):
            if name not in kwargs and name not in defaults:
                continue
            if accepts_var_kw or name in params:
                call_kwargs[name] = kwargs.get(name, defaults.get(name))
        # A dspy.Module host runs through its own ``__call__`` (callbacks, usage),
        # as the turn runs a real built module.
        if _is_dspy_module(self._base_agent):
            return self._base_agent(**call_kwargs)
        return self._base_agent.forward(**call_kwargs)


class _NativeInputHostAgentBlueprintModule(_HostAgentBlueprintModule):
    """The delegating module for a host whose ``forward`` takes native inputs."""

    def forward(  # type: ignore[override]
        self,
        question: str,
        session_id: str,
        session_mode: str = "chat",
        session_edit_mode: str = "diff",
        cancel_requested: Any | None = None,
        images: list[Any] | None = None,
        files: list[Any] | None = None,
    ) -> Any:
        """Delegate to the host fake's ``forward``, native inputs included."""

        return self._delegate(
            question,
            session_id,
            {
                "session_mode": session_mode,
                "session_edit_mode": session_edit_mode,
                "cancel_requested": cancel_requested,
                "images": images,
                "files": files,
            },
        )


def host_agent_module(base_agent: Any, agent_def: Any) -> _HostAgentBlueprintModule:
    """Build the delegating module for ``base_agent``.

    The module declares ``images``/``files`` exactly when the host's ``forward``
    does (by name or through ``**kwargs``).
    """

    params, accepts_var_kw = _forward_parameters(_host_forward(base_agent))
    if accepts_var_kw or "images" in params:
        return _NativeInputHostAgentBlueprintModule(base_agent, agent_def)
    return _HostAgentBlueprintModule(base_agent, agent_def)


def install_host_agent_executor(monkeypatch: Any) -> None:
    """Route the ONE blueprint-runtime build seam to the host agent's ``forward``.

    Monkeypatches ``clio_agent.gact.app._build_blueprint_dspy_module`` so a default
    session's react ``main`` executes the ``build_app(agent=...)`` host fake
    instead of compiling a real (LM-bound) DSPy react program. The delegating
    module is returned unconditionally so the real, LM-bound builder never runs
    under this fixture.
    """

    from clio_agent.gact import app as gact_app

    monkeypatch.setattr(gact_app, "_build_blueprint_dspy_module", host_agent_module)


class _ScriptedModule:
    """A built module whose forward is a test-supplied script."""

    def __init__(self, script: Callable[..., Any], agent_def: Any) -> None:
        self._script = script
        self.agent_def = agent_def

    def __call__(self, question: str, session_id: str, **kwargs: Any) -> Any:
        """Run the script with the kwargs the turn passed."""

        return self._script(question=question, session_id=session_id, **kwargs)


def install_scripted_module(monkeypatch: Any, script: Callable[..., Any]) -> None:
    """Make every turn build a module that runs ``script``.

    ``script`` receives the kwargs the turn passes to the built module
    (``question``, ``session_id``, ``session_mode``, ``session_edit_mode``,
    ``cancel_requested``) and returns the prediction. It runs in the turn's
    forward executor, so it can stream live text with :func:`emit_live_text`.
    """

    from clio_agent.gact import app as gact_app

    def _builder(base_agent: Any, agent_def: Any) -> _ScriptedModule:
        del base_agent
        return _ScriptedModule(script, agent_def)

    monkeypatch.setattr(gact_app, "_build_blueprint_dspy_module", _builder)


def emit_live_text(
    text: str,
    agent_id: str = "",
    field: str = "answer",
    *,
    kind: str = "chain_of_thought",
) -> None:
    """Stream one delta through the LM token hooks, as a provider call does.

    Call it from inside a built module's forward (the turn's executor). An
    ``agent_id`` enters that expert's react scope (with ``kind`` as its declared
    ``module.kind``) the way a blueprint module's forward does; ``answer`` is
    marked transcript-visible for the call. A ``provider_thinking:<source>``
    field streams as the provider's own thinking.
    """

    from clio_agent.gact import context as ctx
    from clio_agent.runtime import lm_activity

    tokens = []
    if agent_id:
        tokens.append(ctx.set_react_scope(agent_id, kind))
    tokens.append(ctx.set_visible_answer_stream(True))
    try:
        if field.startswith("provider_thinking:"):
            lm_activity.note_lm_provider_thinking_delta(text, provider=field.split(":", 1)[1])
        else:
            lm_activity.note_lm_answer_delta(text, field=field)
    finally:
        for token in reversed(tokens):
            ctx.reset(token)


def runner_module_builder(runner: Callable[..., Any]) -> Callable[[Any, Any], Any]:
    """A module-builder stand-in whose built module calls ``runner``.

    For patching one of the turn's builder seams (``_build_blueprint_dspy_module``,
    ``_build_prompt_user_agent_module``, ``_build_tool_user_agent_module``): the
    turn builds the module and runs it once with the standard forward kwargs, and
    the module calls ``runner(base_agent, agent_def, question, session_id, ...)``
    with whichever of those kwargs ``runner`` declares (e.g. ``cancel_requested``).
    """

    def build(base_agent: Any, agent_def: Any) -> Callable[..., Any]:
        def run(question: str, session_id: str, **kwargs: Any) -> Any:
            params, accepts_var_kw = _forward_parameters(runner)
            extra = {k: v for k, v in kwargs.items() if accepts_var_kw or k in params}
            return runner(base_agent, agent_def, question, session_id, **extra)

        return run

    return build
