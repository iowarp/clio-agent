"""Live-streaming + prediction-rendering cluster (#714 decomposition).

This module owns the runtime plumbing that turns an agent ``forward`` into a
live token stream, plus the helpers that render a finished prediction onto the
wire:

* signature-compatible agent invocation (:func:`_agent_forward_compat`,
  :func:`_try_streamed_forward_compat`, :func:`_run_dynamic_agent_compat`) that
  thread new optional kwargs (``session_mode``/``session_edit_mode``/``images``/
  ``cancel_requested``) while falling back to legacy signatures for test fakes
  and older builds;
* the DSPy ``streamify`` pump (:func:`_try_streamed_forward`) that emits every
  text chunk through ``emit_chunk`` as it arrives, with reasoning-channel
  heartbeats and a structured fallback ledger
  (:class:`_StreamingOutputError`, :func:`_stream_fallback_payload`,
  :func:`_stream_fallback_reasons`, :func:`_record_stream_fallback`,
  :func:`_pop_stream_fallback`);
* stream-listener binding (:func:`_append_stream_listener`,
  :func:`_build_stream_listeners`) and streamability gating
  (:func:`_agent_streaming_unsupported_reason`,
  :func:`_config_is_reasoning_model`);
* chunk/text extraction + formatting (:func:`_chunk_text`,
  :func:`_chunk_reasoning_text`, :func:`_stream_response_prefix`,
  :func:`_describe_stream_exc`);
* prediction rendering for the wire (:func:`_format_react_trajectory`,
  :func:`_extract_tools_called`, :func:`_signature_prompt`).

These were carved out of ``gact/app.py`` verbatim (pure move, behavior
preserved). ``app.py`` re-exports every symbol so existing
``from clio_agent.gact.app import <name>`` callers + test seams stay green.

``_try_streamed_forward_compat`` resolves ``_try_streamed_forward`` through the
``clio_agent.gact.app`` re-export at call time so the existing test seam
(``monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", ...)``)
keeps intercepting the turn path unchanged.
"""

from __future__ import annotations

import inspect
import time
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact.events import Event
from clio_agent.gact.evidence import _bounded_tool_call_result
from clio_agent.gact.providers.config import _provider_runtime_kind
from clio_agent.gact.runtime.capabilities import _STREAM_FALLBACK_REASON_DEFINITIONS
from clio_agent.runtime import trace
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from fastapi import FastAPI


def _agent_forward_compat(
    agent: Any,
    question: str,
    session_id: str,
    session_mode: str,
    session_edit_mode: str,
    cancel_requested: Any | None = None,
    images: list[Any] | None = None,
) -> Any:
    """Call agent.forward, threading session_mode + session_edit_mode
    when the agent accepts them, falling back to the legacy
    ``(question, session_id)`` signature for fakes / older builds.

    Lets us add new optional kwargs to the contract without breaking
    every test fixture that hand-rolled a minimal forward signature.
    """

    optional_kwargs: dict[str, Any] = {
        "images": images or [],
        "cancel_requested": cancel_requested,
    }
    attempts = [
        optional_kwargs,
        {"cancel_requested": cancel_requested},
        {"images": images or []},
        {},
    ]
    last_type_error: TypeError | None = None
    for optional in attempts:
        try:
            return agent.forward(
                question,
                session_id=session_id,
                session_mode=session_mode,
                session_edit_mode=session_edit_mode,
                **optional,
            )
        except TypeError as exc:
            message = str(exc)
            if "images" not in message and "cancel_requested" not in message:
                last_type_error = exc
                break
            last_type_error = exc

    try:
        return agent.forward(question, session_id=session_id)
    except TypeError as exc:
        if last_type_error is not None:
            raise last_type_error from exc
        raise


async def _try_streamed_forward_compat(
    app: "FastAPI",
    enriched_text: str,
    sid: str,
    emit_chunk: Any,
    *,
    session_mode: str = "chat",
    session_edit_mode: str = "diff",
    images: list[Any] | None = None,
    agent_override: Any | None = None,
    cancel_requested: Any | None = None,
) -> Optional[Any]:
    """Call _try_streamed_forward with a legacy-signature fallback for tests/plugins."""

    # Resolve through the app re-export so the test seam
    # ``monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", ...)``
    # keeps intercepting the turn path after the streaming extraction.
    from clio_agent.gact.app import _try_streamed_forward  # noqa: PLC0415

    base_kwargs: dict[str, Any] = {
        "session_mode": session_mode,
        "session_edit_mode": session_edit_mode,
    }
    if agent_override is not None:
        base_kwargs["agent_override"] = agent_override

    optional_attempts: list[dict[str, Any]] = [
        {"images": images or [], "cancel_requested": cancel_requested},
        {"cancel_requested": cancel_requested},
        {"images": images or []},
        {},
    ]
    last_type_error: TypeError | None = None
    for optional in optional_attempts:
        try:
            return await _try_streamed_forward(
                app,
                enriched_text,
                sid,
                emit_chunk,
                **base_kwargs,
                **optional,
            )
        except TypeError as exc:
            message = str(exc)
            if "cancel_requested" not in message and "images" not in message:
                raise
            last_type_error = exc
    if last_type_error is not None:
        raise last_type_error
    return None


def _run_dynamic_agent_compat(
    runner: Any,
    base_agent: Any,
    dynamic_agent: Any,
    question: str,
    sid: str,
    cancel_requested: Any | None,
) -> Any:
    """Run a dynamic agent while preserving older runner call signatures."""

    try:
        return runner(base_agent, dynamic_agent, question, sid, cancel_requested)
    except TypeError as exc:
        if "positional" not in str(exc) and "argument" not in str(exc):
            raise
        return runner(base_agent, dynamic_agent, question, sid)


class _StreamingOutputError(RuntimeError):
    """Raised when live streaming fails after user-visible output was emitted."""


def _stream_fallback_payload(reason: str, message: str = "") -> dict[str, Any]:
    """Build structured metadata for a batch text delivery path."""

    definition = _STREAM_FALLBACK_REASON_DEFINITIONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown stream fallback reason: {reason}")
    payload: dict[str, Any] = {
        "reason": reason,
        **{
            key: list(value) if isinstance(value, list) else value
            for key, value in definition.items()
        },
    }
    if message:
        payload["message"] = message
    return payload


def _stream_fallback_reasons(app: "FastAPI") -> dict[str, dict[str, Any]]:
    reasons = getattr(app.state, "stream_fallback_reasons", None)
    if not isinstance(reasons, dict):
        reasons = {}
        app.state.stream_fallback_reasons = reasons
    return reasons


def _record_stream_fallback(
    app: "FastAPI",
    sid: str,
    reason: str,
    message: str = "",
) -> None:
    _stream_fallback_reasons(app)[sid] = _stream_fallback_payload(reason, message)


def _pop_stream_fallback(app: "FastAPI", sid: str) -> dict[str, Any]:
    return _stream_fallback_reasons(app).pop(sid, {})


def _append_stream_listener(
    listeners: list[Any],
    stream_listener_cls: Any,
    *,
    signature_field_name: str,
    predict: Any,
) -> bool:
    if predict is None:
        return False
    try:
        listeners.append(
            stream_listener_cls(
                signature_field_name=signature_field_name,
                predict=predict,
            )
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def _stream_predictor_candidates(agent: Any) -> list[Any]:
    """Return likely DSPy predictor objects attached to a CLIO module.

    Blueprint modules wrap predictors in a few different shapes:
    ``program`` for predict/CoT experts, ``react_agent.extract.predict`` for
    ReAct extractors, and the older top-level ``chat_agent`` /
    ``answer_synthesizer`` fields. StreamListener wants the underlying predictor,
    so walk those known wrapper attributes generically instead of special-casing
    one blueprint or provider.
    """

    candidates: list[Any] = []
    seen: set[int] = set()
    stack: list[Any] = [agent]
    attribute_names = (
        "chat_agent",
        "answer_synthesizer",
        "program",
        "predict",
        "extract",
        "react_agent",
    )

    while stack:
        current = stack.pop()
        if current is None:
            continue
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        candidates.append(current)
        for attr_name in reversed(attribute_names):
            try:
                child = getattr(current, attr_name, None)
            except Exception:  # noqa: BLE001 - third-party wrappers may expose properties
                continue
            if child is not None and id(child) not in seen:
                stack.append(child)
    return candidates


def _build_stream_listeners(agent: Any, stream_listener_cls: Any) -> list[Any]:
    """Build explicit DSPy stream listeners for CLIO predictors.

    Auto-discovering by field name is fragile here because several CLIO
    predictors expose the same output fields. Binding each listener to the
    concrete predictor object lets chat, final synthesis, and blueprint expert
    outputs stream live without fighting over repeated names like ``answer``.
    """

    listeners: list[Any] = []
    bound_predictors: set[int] = set()
    root_identity = id(agent)
    for candidate in _stream_predictor_candidates(agent):
        identity = id(candidate)
        if identity == root_identity:
            continue
        if identity in bound_predictors:
            continue
        if _append_stream_listener(
            listeners,
            stream_listener_cls,
            signature_field_name="answer",
            predict=candidate,
        ):
            bound_predictors.add(identity)
            trace.HF_ON and trace.hot(
                "STREAM-DSPY",
                "listener_bound predictor=%s",
                type(candidate).__name__,
            )

    return listeners


def _agent_streaming_unsupported_reason(agent: Any) -> str:
    """Return a fallback reason when the active provider cannot stream live.

    Only the CLI-backed custom transports (``codex`` JSON-RPC, ``claude_code``
    exec) are genuinely non-streaming. Argonne/ALCF (Sophia + Metis) is a plain
    OpenAI-compatible SSE endpoint: it streams at the provider AND through LiteLLM
    (verified: multi-chunk incremental deltas), so it must NOT be force-classified
    as batch. Hardcoding it here bypassed the streamify pump for EVERY ALCF run
    (iowarp/clio-agent#160). The streamify path below has its own graceful
    try/except fallback to sync, so letting argonne attempt streaming can only
    improve on the previous always-batch behaviour.
    """

    provider_config = getattr(agent, "_provider_config", None)
    provider = str(getattr(provider_config, "provider", "") or "")
    provider_kind = _provider_runtime_kind(provider)
    if provider_kind == "claude_code":
        transport = str(getattr(provider_config, "claude_code_transport", "sdk") or "sdk")
        if transport == "exec":
            return "provider_streaming_unsupported"
    elif provider_kind == "codex":
        return "provider_streaming_unsupported"
    # iowarp/clio-agent#639: normalize the preset id (argonne_sophia/_metis) to
    # the provider kind (argonne) BEFORE the capability check. Reasoning models on
    # the ALCF gateways stream their answer on the reasoning_content channel,
    # which DSPy's content-only stream listeners can't fold and which fails the
    # streamify task group ("live streaming failed before emitting output"). Route
    # them through the robust blocking path (which recovers reasoning_content via
    # _process_completion). Scoped to argonne reasoning models: non-reasoning ALCF
    # (gpt-oss/gemma) still streams (#160), and lm_studio reasoning models (qwopus)
    # stream content fine, so they are untouched.
    if provider_kind == "argonne" and _config_is_reasoning_model(provider_config):
        return "provider_streaming_unsupported"
    return ""


def _config_is_reasoning_model(provider_config: Any) -> bool:
    """Whether a provider config is a reasoning model (handshake ``is_reasoning``
    / per-model capability). Used to keep reasoning models off streaming paths
    that lose the reasoning_content channel."""

    if provider_config is None:
        return False
    try:
        from clio_agent.config import _reasoning_model_capability  # noqa: PLC0415

        return bool(_reasoning_model_capability(provider_config))
    except Exception:
        return bool(getattr(provider_config, "is_reasoning", False))


def _stream_response_prefix(field_name: str, previous_field_name: str) -> str:
    """Return formatting to insert when a streamed output field starts."""

    if not field_name or field_name == previous_field_name:
        return ""
    if field_name == "recommendations":
        return "\n\nRecommendations:\n"
    if field_name == "file_path":
        return "\n\nFile: "
    return ""


# Minimum gap between reasoning-channel heartbeats. The watchdog only needs
# *a* progress event within its window (default 900s), so a 1s throttle keeps a
# deep-reasoning turn alive without flooding the bus with one event per token.
_REASONING_HEARTBEAT_S = 1.0


def _describe_stream_exc(exc: BaseException) -> str:
    """Format a streaming exception for logging, UNWRAPPING ``ExceptionGroup``.

    ``streamify`` runs the agent forward inside an anyio task group, so a failure
    surfaces as ``ExceptionGroup`` whose ``str()`` is only the opaque wrapper
    ("unhandled errors in a TaskGroup (1 sub-exception)") — the real cause lives
    in ``.exceptions``. Recurse into the leaves so the captured detail names the
    actual provider/transport error instead of the wrapper.
    """
    group = getattr(exc, "exceptions", None)
    if group:
        leaves = "; ".join(_describe_stream_exc(sub) for sub in group)
        return f"{type(exc).__name__}[{leaves}]"
    return f"{type(exc).__name__}: {exc}"


async def _try_streamed_forward(
    app: "FastAPI",
    enriched_text: str,
    sid: str,
    emit_chunk,
    session_mode: str = "chat",
    session_edit_mode: str = "diff",
    agent_override: Any | None = None,
    cancel_requested: Any | None = None,
) -> Optional[Any]:
    """Run the agent's forward via dspy.streamify, pumping every
    text chunk through ``emit_chunk(text)`` as it arrives. Returns
    the final dspy.Prediction on success, or None if streaming is
    unavailable before invoking the agent. Streaming execution failures
    raise ``_StreamingOutputError`` so the caller can surface the failed
    turn instead of rerunning it as batch fallback text.

    Falls back before output when the agent isn't a DSPy module, when
    streamify import fails, or when the wrapped call doesn't yield
    parsable text chunks. The fallback synchronous path produces
    the same wire shape (just no live deltas).
    """

    # Guided/structured output streams as reasoning_content-only deltas on
    # LM Studio (no content deltas), which the assembly below can't fold into
    # content -> empty content -> parse failure. Return None so the caller falls
    # back to the blocking path, whose content<-reasoning_content fallback
    # (_process_completion) recovers the constrained JSON. TODO: fold reasoning
    # deltas into the stream assembly to re-enable live streaming under guided output.
    try:
        from clio_agent.config import _guided_output_enabled  # noqa: PLC0415

        if _guided_output_enabled():
            _record_stream_fallback(app, sid, "stream_disabled_guided_output")
            return None
    except Exception:  # noqa: BLE001 - never let this gate break the turn
        pass

    # Some reasoning-model + provider combos stream the answer entirely on the
    # reasoning_content delta channel (which content-only stream listeners miss
    # and which bypasses _process_completion's content<-reasoning_content
    # recovery) or fail the streamify task group outright. Routing them through
    # the blocking path recovers the answer. Default ON (unchanged for every
    # model that streams cleanly); opt out per model via CLIO_LIVE_STREAMING=0.
    try:
        from clio_agent.config import _live_streaming_enabled  # noqa: PLC0415

        if not _live_streaming_enabled():
            _record_stream_fallback(app, sid, "stream_disabled_live_streaming")
            return None
    except Exception:  # noqa: BLE001 - never let this gate break the turn
        pass

    try:
        import dspy  # noqa: PLC0415
        from dspy.streaming.messages import StreamResponse  # noqa: PLC0415
        from dspy.streaming.streamify import streamify
        from dspy.streaming.streaming_listener import StreamListener  # noqa: PLC0415
        from litellm.types.utils import ModelResponseStream  # noqa: F401
    except Exception as exc:
        _record_stream_fallback(
            app,
            sid,
            "streaming_dependency_unavailable",
            f"{type(exc).__name__}: {exc}",
        )
        return None

    agent = agent_override if agent_override is not None else app.state.agent
    if agent is None:
        _record_stream_fallback(app, sid, "agent_not_available")
        return None
    if not isinstance(agent, dspy.Module):
        _record_stream_fallback(app, sid, "agent_not_streamable")
        return None
    unsupported_reason = _agent_streaming_unsupported_reason(agent)
    if unsupported_reason:
        _record_stream_fallback(app, sid, unsupported_reason)
        return None

    # iowarp/clio-agent#158: bind listeners to explicit Predict instances
    # instead of asking DSPy to infer them by output field name.
    listeners = _build_stream_listeners(agent, StreamListener)
    # is_async_program=True is only valid for modules with a real async
    # forward implementation. dspy.Module exposes acall generically, but
    # its default implementation delegates to aforward; ClioAgent only has
    # sync forward today, so treating inherited acall as sufficient forces
    # streamify into AttributeError and silently drops to synthetic fallback.
    has_async_forward = callable(getattr(agent, "aforward", None))
    try:
        streamed = streamify(
            agent,
            async_streaming=True,
            stream_listeners=listeners,
            is_async_program=has_async_forward,
        )
    except Exception as exc:
        # Stream binding is best-effort. If DSPy cannot attach the
        # listener to this program shape, let the canonical sync path
        # run and surface any real agent/provider error from there.
        _record_stream_fallback(
            app,
            sid,
            "stream_setup_failed",
            f"{type(exc).__name__}: {exc}",
        )
        return None

    final_pred = None
    emitted_any = False
    previous_stream_field = ""
    provider_event_index = 0
    # Seed the reasoning-heartbeat clock so the first reasoning chunk publishes
    # immediately (refreshing the watchdog the moment the model starts thinking).
    last_reasoning_heartbeat = time.monotonic() - _REASONING_HEARTBEAT_S

    async def _emit_visible_chunk(text: str, field_name: str = "") -> None:
        nonlocal emitted_any, previous_stream_field
        prefix = _stream_response_prefix(field_name, previous_stream_field)
        if prefix:
            await emit_chunk(prefix)
            emitted_any = True
        await emit_chunk(text)
        emitted_any = True
        if field_name:
            previous_stream_field = field_name

    try:
        # StreamListener emits ``StreamResponse`` instances that
        # carry the cleaned chunk in ``.chunk``. Keep the legacy
        # ``ModelResponseStream`` / dict / str fallback for backends
        # that don't surface a typed listener payload.
        # Pass session_mode + session_edit_mode if the agent's
        # forward signature accepts them (newer ClioAgent does;
        # older / fake agents fall back via TypeError catch).
        try:
            stream_iter = streamed(
                question=enriched_text,
                session_id=sid,
                session_mode=session_mode,
                session_edit_mode=session_edit_mode,
                cancel_requested=cancel_requested,
            )
        except TypeError:
            try:
                stream_iter = streamed(
                    question=enriched_text,
                    session_id=sid,
                    session_mode=session_mode,
                    session_edit_mode=session_edit_mode,
                )
            except TypeError:
                stream_iter = streamed(question=enriched_text, session_id=sid)
        async for piece in stream_iter:
            provider_event_index += 1
            if isinstance(piece, StreamResponse):
                piece_text = str(piece.chunk or "")
                piece_reasoning = ""
                source_channel = "contract_delta" if piece_text else "provider_event"
                signature_field = getattr(piece, "signature_field_name", "") or ""
            elif isinstance(piece, dspy.Prediction):
                piece_text = ""
                piece_reasoning = ""
                source_channel = "final_prediction"
                signature_field = ""
            else:
                piece_text = _chunk_text(piece)
                piece_reasoning = _chunk_reasoning_text(piece)
                if piece_reasoning:
                    source_channel = "reasoning_content"
                elif piece_text:
                    source_channel = "text_delta"
                else:
                    source_channel = "provider_event"
                signature_field = ""
            stream_audit(
                "provider.raw_event",
                provider="dspy_streamify",
                session_id=sid,
                event_index=provider_event_index,
                raw_event_type=type(piece).__name__,
                source_channel=source_channel,
                signature_field_name=signature_field,
                text_len=len(piece_text),
                reasoning_len=len(piece_reasoning),
                chunk_len=len(piece_text or piece_reasoning),
                head=(piece_text or piece_reasoning)[:120],
            )
            trace.HF_ON and trace.hot(
                "STREAM-DSPY",
                "piece type=%s final=%s",
                type(piece).__name__,
                isinstance(piece, dspy.Prediction),
            )
            if isinstance(piece, dspy.Prediction):
                final_pred = piece
                continue
            if isinstance(piece, StreamResponse):
                if piece.chunk:
                    trace.HF_ON and trace.hot(
                        "STREAM-DSPY",
                        "stream_response field=%s len=%d head=%r",
                        getattr(piece, "signature_field_name", "") or "",
                        len(piece.chunk),
                        piece.chunk[:80],
                    )
                    await _emit_visible_chunk(
                        piece.chunk, getattr(piece, "signature_field_name", "") or ""
                    )
                continue
            text_chunk = _chunk_text(piece)
            if text_chunk:
                trace.HF_ON and trace.hot(
                    "STREAM-DSPY",
                    "raw_chunk len=%d head=%r",
                    len(text_chunk),
                    text_chunk[:80],
                )
                await _emit_visible_chunk(text_chunk)
                continue
            # No answer-content in this chunk -- but the model may be actively
            # streaming REASONING tokens (a separate delta channel invisible to
            # DSPy's content-only listeners). Publishing a throttled, session-
            # scoped heartbeat refreshes the no-progress watchdog so a deep-
            # reasoning expert call isn't killed mid-think. We DON'T route the
            # reasoning into the answer part (it would pollute the answer); the
            # event carries it under a distinct type a TUI may render as
            # "thinking", and -- crucially -- advances bus.last_publish_monotonic.
            reasoning_chunk = _chunk_reasoning_text(piece)
            if reasoning_chunk:
                now = time.monotonic()
                if now - last_reasoning_heartbeat >= _REASONING_HEARTBEAT_S:
                    last_reasoning_heartbeat = now
                    try:
                        app.state.bus.publish(
                            Event(
                                type="agent.reasoning.delta",
                                session_id=sid,
                                payload={"stream_source": "reasoning"},
                            )
                        )
                    except Exception:  # noqa: BLE001 - heartbeat is best-effort
                        pass
    except Exception as exc:
        detail = _describe_stream_exc(exc)
        if emitted_any:
            raise _StreamingOutputError(
                f"live streaming failed after emitting output: {detail}"
            ) from exc
        _record_stream_fallback(
            app,
            sid,
            "stream_failed_before_output",
            detail,
        )
        raise _StreamingOutputError(
            f"live streaming failed before emitting output: {detail}"
        ) from exc
    if emitted_any and final_pred is None:
        raise _StreamingOutputError(
            "live streaming ended after emitting output without a final prediction"
        )
    if final_pred is None:
        _record_stream_fallback(app, sid, "stream_no_prediction")
    elif not emitted_any:
        _record_stream_fallback(
            app,
            sid,
            "stream_completed_without_chunks",
            "DSPy streamify returned a final prediction but emitted no visible text chunks.",
        )
    return final_pred


def _chunk_reasoning_text(piece: Any) -> str:
    """Pull reasoning-channel text out of a streamify chunk.

    Reasoning models (qwopus, nemotron, …) stream their chain-of-thought on a
    SEPARATE delta channel (``delta.reasoning_content`` / ``delta.reasoning``),
    not ``delta.content``. DSPy's StreamListener only watches ``delta.content``
    for ``[[ ## field ## ]]`` markers, so reasoning tokens are invisible to it.
    For an unlistened predict (every blueprint expert), streamify yields the raw
    chunk straight through to our pump -- but ``_chunk_text`` returns "" for it
    (content is empty during thinking). We extract the reasoning channel here so
    the pump can refresh the no-progress watchdog while the model is *actively
    thinking* (a deep-reasoning expert call can stream tens of thousands of
    reasoning tokens with zero answer-content tokens; treating that as "no
    progress" wrongly kills a working model -- see the EarthScope resolver hang).
    """

    if not piece or isinstance(piece, (str, dict)):
        # dict shape handled below in the rare OpenAI-dict path; str is answer text.
        if isinstance(piece, dict):
            try:
                delta = piece["choices"][0]["delta"]
                return str(delta.get("reasoning_content") or delta.get("reasoning") or "")
            except (KeyError, IndexError, TypeError):
                return ""
        return ""
    try:
        choices = piece.choices  # type: ignore[attr-defined]
        if choices:
            delta = getattr(choices[0], "delta", None)
            if delta is not None:
                reasoning = getattr(delta, "reasoning_content", None) or getattr(
                    delta, "reasoning", None
                )
                if reasoning:
                    return str(reasoning)
    except Exception:  # noqa: BLE001 - best-effort extraction
        pass
    return ""


def _chunk_text(piece: Any) -> str:
    """Pull a string out of whatever streamify yielded.

    Handles litellm ModelResponseStream + plain str + dict shapes.
    Returns "" when nothing's there (status-message-only chunks
    don't pollute the part body).
    """

    if isinstance(piece, str):
        return piece
    # litellm stream chunks: choices[0].delta.content
    try:
        choices = piece.choices  # type: ignore[attr-defined]
        if choices:
            delta = getattr(choices[0], "delta", None)
            if delta is not None:
                content = getattr(delta, "content", None)
                if content:
                    return str(content)
    except Exception:
        pass
    if isinstance(piece, dict):
        # OpenAI-style dict.
        try:
            return piece["choices"][0]["delta"].get("content", "") or ""
        except (KeyError, IndexError, TypeError):
            return ""
    return ""


def _format_react_trajectory(traj: Any) -> str:
    """Render a DSPy ReAct trajectory (a list/dict of steps) as a
    human-readable trace. Returns "" when the input doesn't look
    like a trajectory.
    """

    if not traj:
        return ""
    rows: list[str] = []
    if isinstance(traj, dict):
        # ReAct stores as {step_n_thought, step_n_action, ...}
        idx = 0
        while True:
            thought = traj.get(f"step_{idx}_thought") or traj.get(f"thought_{idx}")
            action = traj.get(f"step_{idx}_tool_name") or traj.get(f"action_{idx}")
            if thought is None and action is None:
                break
            row = []
            if thought:
                row.append(f"thought: {thought}")
            if action:
                row.append(f"action: {action}")
            rows.append("  ".join(row))
            idx += 1
    elif isinstance(traj, list):
        for i, step in enumerate(traj):
            if isinstance(step, dict):
                rows.append(f"step {i}: {step}")
            else:
                rows.append(f"step {i}: {step!r}")
    return "\n".join(rows)


def _extract_tools_called(pred: Any) -> list[dict[str, Any]]:
    """Pull an agent prediction's tool-call trace into a wire-shaped
    list.

    The tier-2 experts expose their tool calls on
    ``pred.tools_called`` when the ReAct loop tracks them. Each
    entry is either a ``clio_agent.arc.schema.ToolCall`` (msgspec
    struct), a plain dict, or an object with attribute access —
    handle all three. Fields copied onto the wire when present:
    name, args, ok, duration_ms, cached. All optional.
    """

    raw = getattr(pred, "tools_called", None)
    if not raw:
        return []

    out: list[dict[str, Any]] = []
    for call in raw:
        row: dict[str, Any] = {}
        agent_trace_call = False
        if isinstance(call, dict):

            def get(key: str, default: Any = None, _src: Any = call) -> Any:
                return _src.get(key, default)
        else:
            # msgspec structs + DSPy trace records — attribute access.
            def get(key: str, default: Any = None, _src: Any = call) -> Any:
                return getattr(_src, key, default)

            agent_trace_call = (
                hasattr(call, "tool") and hasattr(call, "params") and hasattr(call, "result")
            )

        name = get("name") or get("tool") or ""
        if name:
            row["name"] = str(name)

        args = get("args")
        if args is None:
            args = get("arguments")
        if args is None:
            args = get("params")
        if args is not None:
            row["args"] = args

        status = get("status")
        if status is not None:
            row["ok"] = status not in {"failure", "error", "timeout"}
        elif get("ok") is not None:
            row["ok"] = bool(get("ok"))

        duration_ms = get("duration_ms")
        if duration_ms is not None:
            row["duration_ms"] = float(duration_ms)

        cached = get("cached")
        if cached is not None:
            row["cached"] = bool(cached)

        result = get("result")
        if result is not None:
            row["result"] = _bounded_tool_call_result(result)
            if "ok" not in row and agent_trace_call:
                row["ok"] = not (
                    (isinstance(result, dict) and "error" in result)
                    or (isinstance(result, str) and result.startswith("Error:"))
                )

        telemetry_source = get("telemetry_source") or (
            "agent_trace" if agent_trace_call else "posthoc_prediction"
        )
        row["telemetry_source"] = str(telemetry_source)

        if row:
            out.append(row)
    return out


def _signature_prompt(signature: Any) -> str:
    """Return a cleaned DSPy signature docstring for catalog display."""
    return inspect.cleandoc(getattr(signature, "__doc__", "") or "")
