"""BeforeModel / AfterModel via a per-request ``dspy.LM`` wrapper (P2.4, #1072).

The two model hook events fire at the **LM boundary**, not a turn-level seam: a
turn makes N model requests (every ReAct step, every repair sample), so a
``streaming.py``/``turn_forward.py`` hook would fire ONCE per turn and miss N-1
requests. A custom ``dspy.LM`` injected via ``dspy.context(lm=HookedLM(real_lm))``
gives the correct **per-request** granularity — the whole point of P2.4.

On each model request (``__call__`` / ``acall`` — the entry the DSPy adapter
invokes per LM call) the wrapper:

* fires ``BeforeModel`` with the public :class:`~clio_agent.gact.hooks.wire.ModelRequest`
  (model id, messages, sampling params, tools). The hook may:
  - **synthesize** — return a canned ``llm_response`` so the REAL LM is NEVER called
    (offline replay + caching);
  - **route** — carry a ``model_override`` naming a different LM for this one call
    (the model-agnostic-marketplace payoff: cheap-vs-strong, local-vs-cloud);
  - **modify** — carry a ``request_patch`` that rewrites the outgoing
    messages/params (redact) before the LM sees them;
  - **deny** — block the request with a typed reason;
* calls the real (or routed) LM unless synthesized;
* fires ``AfterModel`` with the response — a hook may observe or REWRITE what enters
  context (never the fact the call ran).

When no ``BeforeModel``/``AfterModel`` hook is configured the wrapper is never
installed (:func:`wrap_lm_with_hooks` returns the inner LM unchanged): pure
pass-through, zero overhead, byte-identical output to today.

Statelessness (RULE 4 — no fifth store): the wrapper rides the existing global
:class:`~clio_agent.gact.hooks.dispatcher.HookDispatcher` and the module-level route
resolver. It holds no per-session state of its own.

The concrete ``HookedLM`` class is built lazily (:func:`hooked_lm_cls`) so it can
subclass ``dspy.BaseLM`` — which ``dspy.Predict`` requires via
``isinstance(lm, BaseLM)`` — without paying a top-level ``import dspy`` on the boot
path (mirrors :func:`clio_agent.lm.io_logging._io_logging_lm_cls`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.gact.hooks.dispatcher import (
    dispatch_after_model,
    dispatch_before_model,
    model_hooks_active,
)
from clio_agent.gact.hooks.wire import (
    HookOutcome,
    ModelRequest,
    record_hook_reason,
)

if TYPE_CHECKING:  # pragma: no cover
    import dspy

_dspy_cache: Any = None


def _dspy() -> Any:
    """Return the dspy module, importing it lazily (mirrors the lm.factory pattern)."""

    global _dspy_cache  # noqa: PLW0603
    if _dspy_cache is None:
        import dspy  # noqa: PLC0415

        _dspy_cache = dspy
    return _dspy_cache


#: A route resolver maps a ``model_override`` name to a concrete ``dspy.BaseLM`` (or
#: ``None`` when it cannot resolve the name — a typed degradation, never a silent
#: reroute). Installed process-wide, mirroring the dispatcher's global wiring so no
#: new store appears (RULE 4).
RouteResolver = Callable[[str], "dspy.BaseLM | None"]
_ROUTE_RESOLVER: RouteResolver | None = None


def install_model_route_resolver(resolver: RouteResolver | None) -> None:
    """Install (or clear) the process-global model-route resolver (P2.4 routing)."""

    global _ROUTE_RESOLVER  # noqa: PLW0603
    _ROUTE_RESOLVER = resolver


def get_model_route_resolver() -> RouteResolver | None:
    """Return the installed process-global route resolver, or ``None``."""

    return _ROUTE_RESOLVER


class HookDeniedModelCall(Exception):
    """A ``BeforeModel`` hook denied an outgoing model request (typed, never silent).

    Distinct from a provider/transport failure: it means a *governance* hook
    refused the request. Carries the merged deny ``reason`` so the block is
    diagnosable and never reads as a model error.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason or "BeforeModel hook denied the model request")
        self.reason = reason


def _coerce_outputs(response: Any) -> list[Any]:
    """Coerce a hook ``llm_response`` into the ``list[dict|str]`` shape DSPy expects.

    ``dspy.BaseLM.__call__`` returns a list of per-choice outputs (a bare string
    when a choice has only text, else a dict). A hook may hand back that list
    directly, a single string, or a single dict — all are normalised to a list so
    the adapter downstream never sees a scalar where it expects a sequence.
    """

    if isinstance(response, (list, tuple)):
        return list(response)
    return [response]


_HOOKED_LM_CLS: Any = None


class _HookedLMBehaviour:
    """The per-request ``BeforeModel``/``AfterModel`` dance over an inner LM.

    Delegates every real behaviour (provider transport, streaming/token-liveness,
    the ``lm.call`` trace, transient retry) to the inner ``dspy.BaseLM``; only the
    hook logic lives here. Combined with ``dspy.BaseLM`` in :func:`hooked_lm_cls`
    so ``isinstance(lm, dspy.BaseLM)`` holds and adapters can read
    ``model``/``kwargs``/``supports_*`` off the wrapper (delegated to the inner).

    Defined at module level (not inside :func:`_hooked_lm_mixin`) because it does
    NOT itself subclass ``dspy.BaseLM`` — it is only combined with ``dspy.BaseLM``
    lazily in :func:`hooked_lm_cls`, so this class carries no top-level ``dspy``
    import cost on the boot path.
    """

    # Mirror ``dspy.LM``'s class attribute so the base-LM typed-migration probe
    # never trips for the wrapper.
    forward_contract = "legacy"

    def __init__(self, inner: Any, *, route_resolver: RouteResolver | None = None) -> None:
        self._inner = inner
        # A per-instance resolver overrides the process-global one (tests + any
        # future per-turn routing table); ``None`` falls back to the global.
        self._route_resolver = route_resolver

    # ----------------------------------------------------------------- #
    # Identity / capability delegation (dspy reads these off the lm).     #
    # ----------------------------------------------------------------- #
    def __getattr__(self, name: str) -> Any:
        # Reached only when normal lookup fails: delegate to the inner LM so
        # ``model``/``kwargs``/``history``/``model_type``/``cache``/
        # ``num_retries``/``callbacks`` and any un-overridden method resolve to
        # the real backend.
        if name in ("_inner", "_route_resolver"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    @property
    def supports_function_calling(self) -> bool:
        return bool(getattr(self._inner, "supports_function_calling", False))

    @property
    def supports_reasoning(self) -> bool:
        return bool(getattr(self._inner, "supports_reasoning", False))

    @property
    def supports_response_schema(self) -> bool:
        return bool(getattr(self._inner, "supports_response_schema", False))

    @property
    def supported_params(self) -> set[str]:
        return set(getattr(self._inner, "supported_params", set()) or set())

    def copy(self, **kwargs: Any) -> Any:
        """Return a wrapper over a copy of the inner LM, preserving the resolver.

        DSPy copies the bound LM in some paths (rollout/temperature bumps).
        Re-wrapping keeps the hook dance alive across a copy while applying
        ``kwargs`` to the REAL inner LM (so per-call config still lands on the
        provider).
        """

        return hooked_lm_cls()(
            self._inner.copy(**kwargs), route_resolver=self._route_resolver
        )

    def forward(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> Any:
        """Delegate ``forward`` to the inner LM (defensive; hot path is ``__call__``)."""

        return self._inner.forward(prompt=prompt, messages=messages, **kwargs)

    async def aforward(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> Any:
        """Delegate ``aforward`` to the inner LM (defensive)."""

        return await self._inner.aforward(prompt=prompt, messages=messages, **kwargs)

    # ----------------------------------------------------------------- #
    # The per-request hook dance.                                        #
    # ----------------------------------------------------------------- #
    def __call__(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> list[Any]:
        request = self._build_request(prompt, messages, kwargs)
        sid, turn_id, cwd = _resolve_call_context()
        before = dispatch_before_model(request, session_id=sid, turn_id=turn_id, cwd=cwd)
        self._enforce_deny(before)
        if self._should_synthesize(before):
            outputs = _coerce_outputs(before.llm_response)
            synthetic = True
        else:
            target, call_messages, call_kwargs = self._resolve_call(before, messages, kwargs)
            outputs = target(prompt=prompt, messages=call_messages, **call_kwargs)
            synthetic = False
        return self._apply_after(request, outputs, synthetic, sid, turn_id, cwd)

    async def acall(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> list[Any]:
        request = self._build_request(prompt, messages, kwargs)
        sid, turn_id, cwd = _resolve_call_context()
        before = dispatch_before_model(request, session_id=sid, turn_id=turn_id, cwd=cwd)
        self._enforce_deny(before)
        if self._should_synthesize(before):
            outputs = _coerce_outputs(before.llm_response)
            synthetic = True
        else:
            target, call_messages, call_kwargs = self._resolve_call(before, messages, kwargs)
            outputs = await target.acall(prompt=prompt, messages=call_messages, **call_kwargs)
            synthetic = False
        return self._apply_after(request, outputs, synthetic, sid, turn_id, cwd)

    # ----------------------------------------------------------------- #
    # Helpers (kept tiny so __call__/acall read as the contract).       #
    # ----------------------------------------------------------------- #
    def _build_request(
        self, prompt: Any, messages: Any, kwargs: Mapping[str, Any]
    ) -> ModelRequest:
        """Assemble the public :class:`ModelRequest` a hook inspects (creds stripped)."""

        if messages is not None:
            msgs = list(messages)
        elif prompt is not None:
            msgs = [{"role": "user", "content": prompt}]
        else:
            msgs = []
        tools = list(kwargs.get("tools") or [])
        params = {
            key: value
            for key, value in kwargs.items()
            if key != "tools" and not key.startswith("api_")
        }
        return ModelRequest(
            model=str(getattr(self._inner, "model", "") or ""),
            messages=msgs,
            params=params,
            tools=tools,
        )

    @staticmethod
    def _enforce_deny(before: HookOutcome) -> None:
        if before.denied:
            record_hook_reason("hook_model_denied", deny_reason=before.reason)
            raise HookDeniedModelCall(before.reason)

    @staticmethod
    def _should_synthesize(before: HookOutcome) -> bool:
        """Whether a BeforeModel hook truly skips the real LM (synthesize + response)."""

        if before.decision != "synthesize":
            return False
        if before.llm_response_present:
            return True
        # A synthesize with no ``llm_response`` cannot skip the call — record the
        # typed degradation and let the real LM run (no silent fail-open).
        record_hook_reason("hook_synthesize_missing_llm_response")
        return False

    def _resolve_call(
        self, before: HookOutcome, messages: Any, kwargs: Mapping[str, Any]
    ) -> tuple[Any, Any, dict[str, Any]]:
        """Resolve the (target LM, messages, kwargs) for a real call after BeforeModel.

        Applies routing (``model_override`` → an alternate LM, or a typed
        ``hook_route_unresolved`` degradation falling back to the default) and a
        ``request_patch`` redact (rewrites messages/params) before the call.
        """

        target = self._inner
        if before.model_override:
            routed = self._resolve_route(before.model_override)
            if routed is not None:
                target = routed
            else:
                record_hook_reason(
                    "hook_route_unresolved", model_override=before.model_override
                )
        call_messages = messages
        call_kwargs = dict(kwargs)
        if before.has_request_patch and before.request_patch is not None:
            patch = before.request_patch
            if "messages" in patch:
                call_messages = list(patch["messages"])
            patch_params = patch.get("params")
            if isinstance(patch_params, Mapping):
                call_kwargs.update(patch_params)
        return target, call_messages, call_kwargs

    def _resolve_route(self, name: str) -> Any:
        """Resolve a ``model_override`` name via the per-instance/global resolver."""

        resolver = self._route_resolver or _ROUTE_RESOLVER
        if resolver is None:
            return None
        return resolver(name)

    def _apply_after(
        self,
        request: ModelRequest,
        outputs: list[Any],
        synthetic: bool,
        sid: str,
        turn_id: str,
        cwd: str,
    ) -> list[Any]:
        """Fire ``AfterModel`` and apply a response rewrite (what enters context only)."""

        after = dispatch_after_model(
            request,
            response=outputs,
            synthetic=synthetic,
            session_id=sid,
            turn_id=turn_id,
            cwd=cwd,
        )
        if after.llm_response_present:
            return _coerce_outputs(after.llm_response)
        return outputs


def _hooked_lm_mixin() -> type:
    """Return the behaviour mixin — the per-request hook dance, no base LM.

    The mixin is a module-level class (:class:`_HookedLMBehaviour`); this
    function just names the seam :func:`hooked_lm_cls` calls to fetch it,
    preserving the lazy-build entry point without needing a class body defined
    per call.
    """

    return _HookedLMBehaviour


def hooked_lm_cls() -> Any:
    """Build (once) the concrete ``HookedLM`` = behaviour mixin + ``dspy.BaseLM``.

    The mixin precedes ``dspy.BaseLM`` in the MRO so its ``__call__``/``acall``/
    capability properties win, while ``isinstance(lm, dspy.BaseLM)`` still holds.
    """

    global _HOOKED_LM_CLS  # noqa: PLW0603
    if _HOOKED_LM_CLS is not None:
        return _HOOKED_LM_CLS
    dspy = _dspy()
    mixin = _hooked_lm_mixin()
    _HOOKED_LM_CLS = type("HookedLM", (mixin, dspy.BaseLM), {"forward_contract": "legacy"})
    return _HOOKED_LM_CLS


def build_hooked_lm(inner: Any, *, route_resolver: RouteResolver | None = None) -> Any:
    """Construct a ``HookedLM`` over ``inner`` unconditionally (tests / explicit wiring)."""

    return hooked_lm_cls()(inner, route_resolver=route_resolver)


def create_hooked_lm(config: Any, *, route_resolver: RouteResolver | None = None) -> Any:
    """Build a provider LM and wrap it for BeforeModel/AfterModel in one call (P2.4).

    A drop-in for ``create_lm`` at the ``dspy.context(lm=...)`` sites: constructs the
    real LM through the ``clio_agent.config.create_lm`` seam (so the test monkeypatch
    point is honoured) and applies :func:`wrap_lm_with_hooks` — which is pure
    pass-through (returns the inner LM unchanged) when no model hook is configured.
    """

    from clio_agent.config import create_lm  # noqa: PLC0415 - honour the monkeypatch seam

    return wrap_lm_with_hooks(create_lm(config), route_resolver=route_resolver)


def wrap_lm_with_hooks(lm: Any, *, route_resolver: RouteResolver | None = None) -> Any:
    """Wrap ``lm`` in a ``HookedLM`` iff a model hook is configured (P2.4).

    The pass-through fast path: when no ``BeforeModel``/``AfterModel`` hook exists the
    inner LM is returned UNCHANGED — the wrapper is never constructed, so a turn with
    no model hook pays zero overhead and produces identical output. Call it at every
    ``dspy.context(lm=...)`` site around the forward path.
    """

    if not model_hooks_active():
        return lm
    return build_hooked_lm(lm, route_resolver=route_resolver)


def _resolve_call_context() -> tuple[str, str, str]:
    """Best-effort (session_id, turn_id, cwd) for the model envelope.

    Resolved from the GACT turn contextvars (the same seam ``io_logging`` reads),
    so no plumbing has to thread session identity through every ``dspy.context``
    site. Absent a live turn (CLI/optimizer) all three are empty — the hook still
    fires with the model request, just without session provenance.
    """

    try:
        from clio_agent.gact.context import (  # noqa: PLC0415
            active_session_id,
            active_turn_id,
        )
    except Exception:  # noqa: BLE001 - app layer unavailable (CLI/optimizer path)
        return "", "", ""
    try:
        return active_session_id() or "", active_turn_id() or "", ""
    except Exception:  # noqa: BLE001 - never let context resolution break a call
        return "", "", ""
