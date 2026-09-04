"""Runtime LM behavior: the I/O-logging ``dspy.LM`` subclass + transient-retry
helpers.

Extracted from :mod:`clio_agent.config` (#769). ``clio_agent.config`` re-exports
every public name here so historical ``from clio_agent.config import X`` seams
(and their monkeypatch points) keep working; new code should import from
:mod:`clio_agent.lm.io_logging` directly.
"""

from __future__ import annotations

from typing import Any

from clio_agent.lm.adapters import _guided_output_enabled
from clio_agent.runtime.stream_audit import stream_audit

_dspy_cache = None


def _dspy():
    """Return the dspy module, importing it on first call (memoised).

    Mirrors ``clio_agent.config._dspy``: dspy is imported lazily because a
    top-level ``import dspy`` costs several seconds on some frameworks Pythons
    and this module is on hot boot paths.
    """
    global _dspy_cache  # noqa: PLW0603
    if _dspy_cache is None:
        import dspy  # noqa: PLC0415

        _dspy_cache = dspy
    return _dspy_cache


class _StreamingPlumbingError(Exception):
    """Internal: token-liveness streaming could not be set up (anyio/dspy
    unavailable, or an event-loop plumbing fault). Signals ``IOLoggingLM.__call__``
    to fall back to the blocking call WITHOUT a second LM round-trip. Never
    raised for a real LM/provider error -- those propagate to the repair loop."""


def _token_liveness_enabled() -> bool:
    """Whether expert LM calls stream so each token refreshes the no-progress
    watchdog (token-liveness). Default ON; kill switch CLIO_LM_TOKEN_LIVENESS=0.

    The mechanism (see IOLoggingLM._clio_streamed_call) only engages for
    synchronous calls outside a running event loop -- i.e. the executor-run expert
    calls -- and defers to the normal blocking path everywhere else.

    Force-OFF under guided output: a guided/structured response streams as
    ``reasoning_content``-only deltas (no ``content`` deltas) on LM Studio, which
    the stream assembly can't fold into content -> empty content -> parse failure.
    The blocking path applies the ``content<-reasoning_content`` fallback
    (``_process_completion``), so guided output uses blocking calls. (TODO: fold
    reasoning deltas into the stream assembly to re-enable liveness here.)
    """
    if _guided_output_enabled():
        return False
    try:
        from clio_agent.conf import as_bool, resolve  # noqa: PLC0415

        return bool(
            resolve(
                "runtime.lm_token_liveness",
                env="CLIO_LM_TOKEN_LIVENESS",
                default=True,
                cast=as_bool,
            )
        )
    except Exception:  # noqa: BLE001 - never let config break LM construction; default on
        return True


# Substrings (matched against the exception's class-name chain AND its message)
# that identify a TRANSIENT provider failure worth retrying: a local model process
# crashing mid-inference (LM Studio "the model has crashed" -> MidStreamFallbackError),
# a dropped connection, a 503/overloaded backend, or a request timeout. Typed-output
# / adapter-parse / validation errors are deliberately ABSENT -- those are not
# transient; the extract/repair loop owns them and they must not be retried here.
_TRANSIENT_PROVIDER_MARKERS = (
    "midstreamfallback",
    "apiconnectionerror",
    "serviceunavailable",
    "internalservererror",
    "apitimeouterror",
    "timeout",  # httpx ReadTimeout/ConnectTimeout/TimeoutException, litellm.Timeout
    "the model has crashed",
    "connection error",
    "remote end closed connection",
    "connection reset",
    "overloaded",
    # claude_code SDK: the pooled CLI subprocess died mid-stream (exit 1, no
    # structured result). Typed at the provider boundary as transient so the
    # call re-issues on a fresh pooled connection (#891). Keep in sync with
    # providers.claude_code_sessions.TRANSIENT_TRANSPORT_MARKER.
    "claude agent sdk transport failed mid-stream",
    # claude_code SDK: the pooled entry was torn down (#1305 deterministic
    # per-subagent release, F6b) while this call was still holding it from an
    # earlier entry_for() -- a fresh entry_for() on retry reconnects cleanly.
    # Keep in sync with providers.claude_code_lifecycle.DEAD_ENTRY_MARKER.
    "claude agent sdk entry released during a queued connect",
)


def _is_transient_provider_error(exc: BaseException) -> bool:
    """True for transient provider/infrastructure failures that a re-issue can heal
    (vs. typed-output/parse errors, which are the repair loop's job, not retried)."""
    names = " ".join(base.__name__.lower() for base in type(exc).__mro__)
    text = f"{names} {exc}".lower()
    return any(marker in text for marker in _TRANSIENT_PROVIDER_MARKERS)


def _lm_transient_retries() -> int:
    """Bounded retries for a transient provider failure (default 2)."""
    try:
        from clio_agent.conf import as_float, resolve  # noqa: PLC0415

        return max(
            0,
            int(
                resolve(
                    "limits.lm_transient_retries",
                    env="CLIO_LM_TRANSIENT_RETRIES",
                    default=2.0,
                    cast=as_float,
                )
            ),
        )
    except Exception:  # noqa: BLE001 - never let config break a call
        return 2


def _lm_transient_backoff_s() -> float:
    """Backoff before re-issuing after a transient failure (default 8s -- enough for
    LM Studio to JIT-reload a crashed local model on the next request)."""
    try:
        from clio_agent.conf import as_float, resolve  # noqa: PLC0415

        value = resolve(
            "limits.lm_transient_backoff_s",
            env="CLIO_LM_TRANSIENT_BACKOFF_S",
            default=8.0,
            cast=as_float,
        )
        return value if value >= 0 else 8.0
    except Exception:  # noqa: BLE001 - never let config break a call
        return 8.0


_IO_LOGGING_LM_CLS: Any = None


def _io_logging_lm_cls() -> Any:
    """Build (once) a dspy.LM subclass that logs every call's full I/O."""
    global _IO_LOGGING_LM_CLS  # noqa: PLW0603
    if _IO_LOGGING_LM_CLS is not None:
        return _IO_LOGGING_LM_CLS
    dspy = _dspy()

    class IOLoggingLM(dspy.LM):  # type: ignore[name-defined,misc]
        """dspy.LM that emits a durable ``lm.call`` trace event per call.

        Reads ``history[-1]`` after each call (same thread as the call), so it
        captures the raw ``content`` AND ``reasoning_content`` channels even when
        the response was truncated or failed downstream parsing -- the one place
        an expert call's reasoning is reliably visible (expert LMs run in
        executors the settle path cannot reach). The happy path is unchanged.
        The canonical trace is the single recorder (no separate JSONL mirror).
        """

        def __call__(self, prompt=None, messages=None, **kwargs):  # type: ignore[override]
            # LM Studio response_format shim (guided output only). DSPy's
            # JSONAdapter sends ``response_format={"type":"json_object"}`` for any
            # signature with an open-ended field (qwopus experts: main's
            # delegation/workflow_state, ReAct's next_tool_args:dict). LM Studio
            # REJECTS json_object ("'response_format.type' must be 'json_schema'
            # or 'text'"), 400-ing the call. Translate it to a permissive
            # json_schema (constrain to a valid JSON object -- json_object
            # semantics -- in the form LM Studio accepts). Strict per-signature
            # schemas (clean signatures) already flow through as pydantic models
            # and are untouched. No-op when guided output is off.
            if _guided_output_enabled():
                _rf = kwargs.get("response_format")
                if isinstance(_rf, dict) and _rf.get("type") == "json_object":
                    kwargs["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "output",
                            "strict": False,
                            "schema": {"type": "object", "additionalProperties": True},
                        },
                    }
            # Bounded retry on TRANSIENT provider failures -- e.g. a local model
            # process crashing mid-inference (LM Studio "the model has crashed" ->
            # MidStreamFallbackError), a dropped connection, or a 503. These abort a
            # turn that is otherwise healthy (here: the parent crashed while routing,
            # AFTER the catalog had already ranked 71 stations). A short backoff lets
            # the provider JIT-reload the crashed model and the call is re-issued.
            # Typed-output/parse/validation errors are NOT transient -- the extract
            # repair loop owns those -- and propagate immediately on the first try.
            attempts = _lm_transient_retries() + 1
            last_exc: BaseException | None = None
            for attempt in range(attempts):
                try:
                    return self._clio_invoke_once(prompt, messages, **kwargs)
                except BaseException as exc:  # noqa: BLE001 - re-raised unless transient
                    last_exc = exc
                    if attempt + 1 < attempts and _is_transient_provider_error(exc):
                        import time as _time  # noqa: PLC0415

                        # D15: the failed attempt may have ALREADY streamed part or
                        # all of its answer/next_thought text live (the streamed
                        # path flushes per-chunk and on close, both BEFORE this
                        # exception propagates). The retry re-issues the SAME call
                        # through a brand-new field extractor with no memory of
                        # that text, so without this the retry's fresh stream lands
                        # on top of the abandoned attempt's text in the SAME still-
                        # open transcript part instead of replacing it -- an exact
                        # duplicate of whatever streamed before the failure. Discard
                        # it here, before the retry, so the next chunk opens clean.
                        from clio_agent.runtime.lm_activity import (  # noqa: PLC0415
                            note_lm_retry_reset,
                        )

                        note_lm_retry_reset()
                        _time.sleep(_lm_transient_backoff_s())
                        continue
                    raise
            assert last_exc is not None  # unreachable; loop returns or raises
            raise last_exc

        def _clio_invoke_once(self, prompt=None, messages=None, **kwargs):  # type: ignore[no-untyped-def]
            # Token-streaming liveness: when enabled AND this call is synchronous
            # (outside a running event loop -- i.e. an executor-run expert call),
            # drive it streamed so each chunk refreshes the no-progress watchdog.
            # In a running loop (e.g. the Tier-1 streamify path) we defer to the
            # blocking call below so we never nest loops / double-stream. Either
            # path emits the canonical lm.call once via the shared finally.
            try:
                if _token_liveness_enabled() and self._clio_can_stream():
                    try:
                        return self._clio_streamed_call(prompt, messages, **kwargs)
                    finally:
                        self._clio_log_last_call()
            except _StreamingPlumbingError:
                pass  # streaming setup unavailable -> fall through to blocking
            try:
                return super().__call__(prompt=prompt, messages=messages, **kwargs)
            finally:
                self._clio_log_last_call()

        def _process_completion(self, response, merged_kwargs):  # type: ignore[no-untyped-def]
            # Reasoning-model content<-reasoning_content fallback. Reasoning models
            # (qwopus, nemotron, ...) intermittently emit the FULL formatted output
            # into the `reasoning_content` channel and leave `content` EMPTY. dspy's
            # base adapter parses output["text"] (= content); empty -> {} -> every
            # field missing -> ValidationError/AdapterParseError. This is the
            # confirmed dominant cause of qwopus typed-output intermittency (verified
            # live: a json_schema call returned schema-perfect JSON in
            # reasoning_content with content=""). When text is empty but
            # reasoning_content is present, use reasoning_content as the parse text so
            # the adapter parses the actual output (the LenientChatAdapter's
            # json/constructor-repr repair then handles its shape). Normal calls
            # (non-empty content) are untouched.
            outputs = super()._process_completion(response, merged_kwargs)
            # Per-model: only reasoning models (qwopus/qwen ...) route output into
            # reasoning_content and need this extraction. Non-reasoning models never
            # leave content empty, so this is a no-op for them, but gate it
            # explicitly per model (set in create_lm) rather than running globally.
            if not getattr(self, "_clio_reasoning_fallback", True):
                return outputs
            try:
                choices = list(getattr(response, "choices", None) or [])
            except Exception:  # noqa: BLE001 - defensive; fall back to no finish info
                choices = []
            # A legitimate formatted output (reasoning + answer + workflow_state) is a
            # few KB; a runaway/truncated chain-of-thought is 100k+ chars. Substituting
            # a truncated giant CoT as the "output" both fails to parse AND bloats every
            # downstream prompt (observed: a 132k-char finish='length' reasoning blew a
            # delegation output to 280k -> 71k-token prompt -> context overflow). So only
            # fall back to reasoning_content when the response COMPLETED normally
            # (finish != 'length') and is sanely sized.
            _MAX_REASONING_FALLBACK_CHARS = 48000
            patched = []
            for i, out in enumerate(outputs):
                if isinstance(out, dict):
                    text = (out.get("text") or "").strip()
                    rc = out.get("reasoning_content") or ""
                    finish = ""
                    if i < len(choices):
                        ch = choices[i]
                        finish = str(
                            getattr(ch, "finish_reason", None)
                            or (ch.get("finish_reason") if isinstance(ch, dict) else "")
                            or ""
                        )
                    if (
                        not text
                        and rc.strip()
                        and finish != "length"
                        and len(rc) <= _MAX_REASONING_FALLBACK_CHARS
                    ):
                        out = {**out, "text": rc}
                patched.append(out)
            return patched

        @staticmethod
        def _clio_can_stream() -> bool:
            """True only when NOT inside a running event loop.

            ``_clio_streamed_call`` uses ``asyncio.run`` (it owns a fresh loop), so
            it applies to the synchronous executor expert calls and defers to the
            normal blocking path inside any already-running loop.
            """
            import asyncio as _asyncio  # noqa: PLC0415

            try:
                _asyncio.get_running_loop()
            except RuntimeError:
                return True
            return False

        def _clio_streamed_call(self, prompt=None, messages=None, **kwargs):  # noqa: ANN001
            """Run the call STREAMED so each chunk refreshes the watchdog.

            Producer awaits ``self.acall`` with ``dspy.settings.send_stream``
            set; a consumer drains-and-discards each chunk, calling
            ``note_lm_activity`` per token. ``acall`` (NOT ``aforward``) is the
            ``@with_callbacks``-wrapped entry: it fires ``on_lm_start``/``on_lm_end``
            -> ``note_lm_start``/``note_lm_end`` (so the call registers as in-flight
            for the watchdog) + the ``lm.call.started`` marker, and it returns the
            SAME processed outputs as the blocking ``__call__`` (``aforward`` +
            ``_process_lm_response``). The inner ``aforward`` assembles the
            authoritative result (litellm ``stream_chunk_builder``) and updates
            ``self.history`` -- so the shared ``_clio_log_last_call`` finally still
            emits ``lm.call``.

            Real LM errors (raised inside ``aforward``) propagate so the repair loop
            handles them exactly as on the blocking path. Streaming-PLUMBING failures
            (anyio/dspy unavailable) raise ``_StreamingPlumbingError`` so ``__call__``
            falls back to the blocking call -- without a double LM round-trip.

            Version-fragile (public surfaces): dspy.BaseLM.acall (@with_callbacks)
            + dspy.settings send_stream + litellm streaming; anyio memory object
            streams. Gated default-on with the CLIO_LM_TOKEN_LIVENESS kill switch.
            """
            import asyncio as _asyncio  # noqa: PLC0415

            try:
                import time as _time  # noqa: PLC0415

                import anyio as _anyio  # noqa: PLC0415

                from clio_agent.runtime import trace  # noqa: PLC0415
                from clio_agent.runtime.lm_activity import (  # noqa: PLC0415
                    note_lm_activity,
                    note_lm_answer_delta,
                    note_lm_token_event,
                )
                from clio_agent.runtime.lm_stream import (  # noqa: PLC0415
                    AnswerFieldExtractor,
                    extract_delta,
                )
            except Exception as exc:  # noqa: BLE001 - plumbing missing -> blocking
                raise _StreamingPlumbingError from exc

            dspy = _dspy()

            async def _drive() -> Any:
                send: Any
                recv: Any
                send, recv = _anyio.create_memory_object_stream(float("inf"))
                holder: dict[str, Any] = {}

                async def _produce() -> None:
                    try:
                        with dspy.settings.context(send_stream=send):
                            holder["result"] = await self.acall(
                                prompt=prompt, messages=messages, **kwargs
                            )
                    except BaseException as exc:  # noqa: BLE001 - re-raised post-drain
                        holder["exc"] = exc
                    finally:
                        await send.aclose()

                extractors = {
                    "reasoning": AnswerFieldExtractor("reasoning"),
                    "answer": AnswerFieldExtractor("answer"),
                    "next_thought": AnswerFieldExtractor("next_thought"),
                    "workflow_state": AnswerFieldExtractor("workflow_state"),
                }
                visible_contract_fields = {"reasoning", "next_thought", "answer"}
                acc_answer = ""
                acc_reasoning = ""
                last_event = _time.monotonic()
                async with _anyio.create_task_group() as tg:
                    tg.start_soon(_produce)
                    async with recv:
                        async for _chunk in recv:
                            note_lm_activity()  # watchdog liveness (per token)
                            content, reasoning = extract_delta(_chunk)
                            if content:
                                trace.HF_ON and trace.hot(
                                    "STREAM-FIELD",
                                    "raw_content len=%d head=%r",
                                    len(content),
                                    content[:120],
                                )
                                for field_name, extractor in extractors.items():
                                    answer_delta = extractor.feed(content)
                                    if (
                                        answer_delta
                                        and field_name in visible_contract_fields
                                        and not extractor.is_structured()
                                    ):
                                        trace.HF_ON and trace.hot(
                                            "STREAM-FIELD",
                                            "emit field=%s len=%d head=%r",
                                            field_name,
                                            len(answer_delta),
                                            answer_delta[:120],
                                        )
                                        # Preserve exact generated output-field tokens.
                                        note_lm_answer_delta(answer_delta, field=field_name)
                                        acc_answer += answer_delta  # highway gets all
                            if reasoning:
                                acc_reasoning += reasoning
                            # highway event (trace + ARC), coalesced so the durable
                            # stream isn't one event per token.
                            now = _time.monotonic()
                            if now - last_event >= 0.25 and (acc_answer or acc_reasoning):
                                note_lm_token_event(acc_answer, acc_reasoning)
                                acc_answer = ""
                                acc_reasoning = ""
                                last_event = now
                    for field_name, extractor in extractors.items():
                        tail = extractor.flush()
                        if (
                            tail
                            and field_name in visible_contract_fields
                            and not extractor.is_structured()
                        ):
                            trace.HF_ON and trace.hot(
                                "STREAM-FIELD",
                                "flush field=%s len=%d head=%r",
                                field_name,
                                len(tail),
                                tail[:120],
                            )
                            note_lm_answer_delta(tail, field=field_name)
                            acc_answer += tail
                    if acc_answer or acc_reasoning:
                        note_lm_token_event(acc_answer, acc_reasoning)
                if "exc" in holder:
                    raise holder["exc"]
                return holder.get("result")

            try:
                return _asyncio.run(_drive())
            except _StreamingPlumbingError:
                raise
            except BaseException as exc:
                # aforward's own error -> propagate (repair loop owns it). A bare
                # asyncio/anyio plumbing failure also lands here; treat anything
                # that is clearly a loop/runtime plumbing fault as fall-back-able,
                # else propagate so a genuine LM failure is not swallowed.
                if isinstance(exc, RuntimeError) and "loop" in str(exc).lower():
                    raise _StreamingPlumbingError from exc
                raise

        @staticmethod
        def _clio_trace_target() -> Any:
            """Return the active GACT trace target (app, sid, turn, trace, emit)
            or None. Lazily imports app to avoid an import cycle; resolves the
            turn-scoped contextvars copied into the executor running this call."""
            try:
                from clio_agent.gact.context import (  # noqa: PLC0415
                    active_app,
                    active_session_id,
                    active_trace_id,
                    active_turn_id,
                )
                from clio_agent.gact.runtime.globals import _emit_semantic_event  # noqa: PLC0415
            except Exception:  # noqa: BLE001 - app may be unavailable (CLI/optimizer paths)
                return None
            app = active_app()
            sid = active_session_id()
            if app is None or not sid:
                return None
            return (
                app,
                sid,
                active_turn_id(),
                active_trace_id(),
                _emit_semantic_event,
            )

        def _clio_log_last_call(self) -> None:
            try:
                # ONE capture per call. Read ``history[-1]`` exactly once here and
                # stash the reasoning-channel text on the instance so the ReAct loop
                # reuses THIS read (``app._active_lm_last_reasoning``) instead of a
                # second independent ``history[-1]`` read. Done before the trace gate
                # so the stash is populated for every call (the loop runs inside a
                # GACT turn; a non-turn call simply emits no ``lm.call``).
                history = getattr(self, "history", None) or []
                if not history or not isinstance(history[-1], dict):
                    return
                entry = history[-1]
                response = entry.get("response")
                content = reasoning = finish = ""
                choices = getattr(response, "choices", None)
                if choices is None and isinstance(response, dict):
                    choices = response.get("choices")
                if choices:
                    ch0 = choices[0]
                    msg = getattr(ch0, "message", None)
                    if msg is None and isinstance(ch0, dict):
                        msg = ch0.get("message")
                    if msg is not None:
                        content = (
                            getattr(msg, "content", None)
                            if not isinstance(msg, dict)
                            else msg.get("content")
                        ) or ""
                        reasoning = (
                            getattr(msg, "reasoning_content", None)
                            if not isinstance(msg, dict)
                            else msg.get("reasoning_content")
                        ) or ""
                    finish = (
                        getattr(ch0, "finish_reason", None)
                        if not isinstance(ch0, dict)
                        else ch0.get("finish_reason")
                    ) or ""
                # Stash the reasoning from this single read so the react step reuses it.
                self._clio_last_reasoning = str(reasoning or "").strip()
                record = {
                    "model": entry.get("model"),
                    "messages": entry.get("messages") or entry.get("prompt"),
                    "content": content,
                    "content_len": len(str(content)),
                    "reasoning_content": reasoning,
                    "reasoning_len": len(str(reasoning)),
                    "finish_reason": finish,
                    "usage": entry.get("usage"),
                    "timestamp": entry.get("timestamp"),
                }
                try:
                    from clio_agent.gact.context import (  # noqa: PLC0415
                        active_session_id,
                        active_trace_id,
                        active_turn_id,
                    )

                    audit_sid = active_session_id()
                    audit_turn_id = active_turn_id()
                    audit_trace_id = active_trace_id()
                except Exception:  # noqa: BLE001 - audit is best-effort
                    audit_sid = ""
                    audit_turn_id = ""
                    audit_trace_id = ""
                stream_audit(
                    "provider.batch_response",
                    provider="dspy_lm",
                    session_id=audit_sid,
                    turn_id=audit_turn_id,
                    trace_id=audit_trace_id,
                    model=str(record["model"] or ""),
                    source_channel=(
                        "content+reasoning_content"
                        if content and reasoning
                        else ("reasoning_content" if reasoning else "content")
                    ),
                    content_len=len(str(content)),
                    reasoning_len=len(str(reasoning)),
                    chunk_len=len(str(content or reasoning)),
                    finish_reason=finish,
                    head=str(content or reasoning)[:120],
                )
                # No active GACT turn -> nothing to emit (CLI/optimizer paths). The
                # stash above is still set so a synchronous loop can read it, and the
                # batch provider audit above still records timing when enabled.
                target = self._clio_trace_target()
                if target is None:
                    return
                # Emit the canonical trace's DURABLE-ONLY lm.call event: the one
                # place an expert call's raw messages + reasoning_content are
                # reliably visible (expert LMs run in executors the settle path
                # can't reach), captured on the failure path too. detail_level="off"
                # keeps it off SSE/UI. (Legacy CLIO_LOG_LM_IO JSONL mirror removed --
                # the canonical trace is the single recorder.)
                app, sid, turn_id, trace_id, emit = target
                try:
                    emit(
                        app,
                        sid,
                        "lm.call",
                        turn_id=turn_id,
                        trace_id=trace_id,
                        status="completed",
                        summary=f"LM call ({record['finish_reason'] or 'ok'}).",
                        provider={"model_id": str(record["model"] or "")},
                        payload=record,
                        detail_level="off",
                    )
                except Exception as exc:  # noqa: BLE001 - capture must never fail a call
                    # NEVER silent: surfaces e.g. the ARC-as-source fail-loud RuntimeError
                    # (no ARC reachable) without breaking the call.
                    from clio_agent.runtime import trace  # noqa: PLC0415

                    trace.event("LM-CALL-CAPTURE", "lm.call capture/emit failed: %r", exc)
            except Exception as exc:  # noqa: BLE001 - logging is best-effort, never fail a call
                from clio_agent.runtime import trace  # noqa: PLC0415

                trace.event("LM-CALL-CAPTURE", "lm.call logging failed: %r", exc)

    _IO_LOGGING_LM_CLS = IOLoggingLM
    return _IO_LOGGING_LM_CLS
