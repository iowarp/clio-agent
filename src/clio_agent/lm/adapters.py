"""Adapter + malformed-output repair layer for local reasoning models.

Extracted from :mod:`clio_agent.config` (#769). ``clio_agent.config`` re-exports
every public name here so historical import seams keep working; new code should
import from :mod:`clio_agent.lm.adapters` directly.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from clio_agent.config import LMProviderConfig

logger = logging.getLogger(__name__)

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


def _coerce_constructor_repr_to_jsonable(text: str) -> Any:
    """Coerce a Python constructor-repr into a nested dict/list/scalar.

    Some local reasoning models (e.g. qwopus) emit a typed output field as a
    Python constructor call — ``Model(field=val, nested=Sub(a=1, b=[...]))`` —
    instead of JSON, which no DSPy adapter parses. This rewrites that shape into
    plain JSON-able data using ``ast`` (constructor calls -> dicts keyed by their
    keyword args; lists/tuples/sets -> lists; literals as-is). Raises on anything
    that is not such a repr, so the caller can fall back to the original error.
    """
    import ast  # noqa: PLC0415

    node = ast.parse(text.strip(), mode="eval").body

    def conv(n: Any) -> Any:
        if isinstance(n, ast.Call):
            return {kw.arg: conv(kw.value) for kw in n.keywords if kw.arg is not None}
        if isinstance(n, ast.Dict):
            return {conv(k): conv(v) for k, v in zip(n.keys, n.values, strict=False)}
        if isinstance(n, (ast.List, ast.Tuple, ast.Set)):
            return [conv(e) for e in n.elts]
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -conv(n.operand)
        if isinstance(n, ast.Name):
            # A bare identifier where a value was expected. Local models (qwopus)
            # routinely emit unquoted JS-style literals or unquoted string values
            # inside a constructor-repr -- e.g. ``analysis_ready=true`` (JS literal,
            # not Python ``True``) or ``status=staged`` (unquoted string). Python's
            # ast sees these as Name nodes, which ast.literal_eval rejects ("malformed
            # node ... ast.Name") and the whole staging tool-call dies. Map the JS
            # literals and treat any other bare name as its string value. Format-only.
            low = n.id.lower()
            if low == "true":
                return True
            if low == "false":
                return False
            if low in ("none", "null"):
                return None
            return n.id
        if isinstance(n, ast.Attribute):
            # e.g. an enum-ish ``Status.STAGED`` -> use the trailing attribute name.
            return n.attr
        return ast.literal_eval(n)

    return conv(node)


def _unwrap_self_named_envelope(obj: Any, field_name: str) -> Any:
    """Unwrap a structured value a model framed under its own field name.

    Reasoning/small models routinely emit a structured output field's value
    wrapped in a single-key envelope keyed by the field's own name -- e.g. the
    ``workflow_state`` field returned as ``{"workflow_state": {...}}`` instead of
    just ``{...}`` (qwopus copies this shape straight from blueprint examples).
    This is a framing error with no semantic change, so unwrap it. Only triggers
    when the dict has exactly that one key and a structured (dict/list) inner
    value -- never alters a genuine single-key payload of a different name.
    """
    if (
        isinstance(obj, dict)
        and len(obj) == 1
        and field_name in obj
        and isinstance(obj[field_name], (dict, list))
    ):
        return obj[field_name]
    return obj


def _recover_malformed_structured_value(field_name: str, text: str) -> Any:
    """Recover a structured field value from a model's malformed text.

    Handles, in order, the format errors local models produce on JSON-object
    output fields -- all purely structural, no semantic change:

    1. a dropped/extra brace or bracket (``json_repair`` rebalances it),
    2. a Python constructor-repr (``Model(field=...)``) instead of JSON
       (``_coerce_constructor_repr_to_jsonable``),
    3. a self-named envelope (``{"<field>": {...}}``) -- unwrapped.

    Raises if none apply, so the caller can dump + surface the original error.
    """
    import json as _json  # noqa: PLC0415

    obj: Any = None
    try:
        import json_repair  # noqa: PLC0415

        obj = _json.loads(json_repair.repair_json(text))
    except Exception:  # noqa: BLE001 - fall through to constructor-repr
        obj = None
    if obj is None:
        obj = _coerce_constructor_repr_to_jsonable(text)
    return _unwrap_self_named_envelope(obj, field_name)


def _dump_unparseable_completion(
    signature: Any, completion: str, field: str, value: str, error: str
) -> None:
    """Best-effort diagnostic dump of a model completion the adapter could not parse.

    Captures the raw ``content`` the strict + lenient parsers both rejected, plus the
    specific field value that broke, so the model↔adapter format mismatch can be seen
    directly instead of inferred. Gated by ``CLIO_DUMP_UNPARSEABLE`` (a file path);
    no-op when unset. Never raises.
    """
    from clio_agent import conf  # noqa: PLC0415 - keep config.py a leaf module

    path = conf.resolve(
        "debug.dump_unparseable", env="CLIO_DUMP_UNPARSEABLE", default="", cast=conf.as_str
    ).strip()
    if not path:
        return
    try:
        import json as _json  # noqa: PLC0415

        record = {
            "signature": getattr(signature, "__name__", str(signature)),
            "output_fields": list(getattr(signature, "output_fields", {}).keys()),
            "failing_field": field,
            "error": error,
            "failing_value": value,
            "raw_completion": completion,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(record) + "\n")
    except Exception as exc:  # noqa: BLE001 - diagnostic only, never fail a turn
        logger.warning(
            "unparseable-output dump not written "
            "reason=unparseable_dump_write_failed path=%s error=%s",
            path,
            exc,
        )


_LENIENT_CHAT_ADAPTER_CLS: Any = None


def _lenient_chat_adapter_cls() -> Any:
    """Build (once) a ChatAdapter subclass that recovers constructor-repr fields."""
    global _LENIENT_CHAT_ADAPTER_CLS  # noqa: PLW0603
    if _LENIENT_CHAT_ADAPTER_CLS is not None:
        return _LENIENT_CHAT_ADAPTER_CLS
    dspy = _dspy()
    import json as _json  # noqa: PLC0415

    from dspy.adapters.chat_adapter import field_header_pattern  # noqa: PLC0415
    from dspy.adapters.utils import parse_value  # noqa: PLC0415
    from dspy.utils.exceptions import AdapterParseError  # noqa: PLC0415

    class LenientChatAdapter(dspy.ChatAdapter):  # type: ignore[name-defined]
        """ChatAdapter that recovers a structured output field a model emitted as a
        Python constructor-repr (``Model(field=...)``) instead of JSON. The happy
        path is unchanged; recovery only runs when the strict parse fails."""

        def format_conversation_history(self, signature, history_field_name, inputs):  # type: ignore[no-untyped-def]
            """Source the ReActV2 History prefix from the materialized ARC live plane.

            The V2 read seam (#901 S2, design B): before the stock formatter runs,
            point the ``dspy.History`` input at ARC's materialized render (see
            ``clio_agent.gact.agents.reactv2.override_history_inputs_from_arc``), so
            ARC is the single wire source and out-of-band ARC edits change the next
            prompt. A no-op for any signature without a ``dspy.History`` field — in
            clio that is exclusively the ReActV2 react signature — so the classic
            (History-less) wire path is byte-identical and unaffected. When ARC is not
            the source (disabled / no scope / empty / read failure) the passed-in
            History renders unchanged, so a standalone V2 loop still works.
            ``override_history_inputs_from_arc`` is fully guarded internally (a read
            failure records a typed reason and no-ops), so no blind swallow is needed
            here — an unexpected raise is a real bug that must surface, not hide.
            """
            from clio_agent.gact.agents.reactv2 import (  # noqa: PLC0415
                override_history_inputs_from_arc,
            )

            override_history_inputs_from_arc(
                inputs, history_field_name, tuple(signature.input_fields)
            )
            return super().format_conversation_history(signature, history_field_name, inputs)

        def format_assistant_message_content(self, signature, message, missing_field_message=None):  # type: ignore[no-untyped-def]
            """Render NO assistant turn for an output-less history event (#901 S2).

            The #901 append-only wire folds the static task inputs into a HEAD history
            event; on the loop's first call that head is SYNTHETIC — it carries the inputs
            but no output fields yet. Stock ``format_conversation_history`` would render a
            placeholder assistant turn ("Not supplied for this conversation history
            message.") for it, which is a MOVING message that breaks the append-only
            prefix (call 1's placeholder ≠ call 2's real assistant turn). Suppressing the
            assistant turn for a message that carries none of the signature's OUTPUT fields
            keeps call 1 = ``[system, {head}, {closing}]`` a clean prefix of call 2 — so
            every non-first call is a stateful delta, not a boundary reset. Only fires for
            a genuinely output-less message (the synthetic head); every real turn event has
            ``next_thought`` / ``tool_calls`` and renders verbatim through the stock path,
            so no visible-lane content is ever dropped.
            """
            if not any(name in message for name in signature.output_fields):
                return ""
            return super().format_assistant_message_content(
                signature, message, missing_field_message
            )

        def parse(self, signature: Any, completion: str) -> dict:
            try:
                return super().parse(signature, completion)
            except (AdapterParseError, TypeError, ValueError) as primary_exc:
                from clio_agent.runtime.lm_stream import (  # noqa: PLC0415
                    normalize_escaped_section_boundaries,
                )

                normalized_completion = normalize_escaped_section_boundaries(completion)
                if normalized_completion != completion:
                    try:
                        parsed = super().parse(signature, normalized_completion)
                    except (AdapterParseError, TypeError, ValueError):
                        logger.debug(
                            "normalized adapter parse did not recover output; "
                            "continuing with structured-value repair"
                        )
                    else:
                        from clio_agent.runtime import trace  # noqa: PLC0415

                        trace.event(
                            "LENIENT-ADAPTER RECOVERY",
                            "restored escaped ChatAdapter section boundary",
                        )
                        return parsed
                # Re-section exactly like ChatAdapter, but coerce a failing field's
                # constructor-repr value into JSON before re-parsing it.
                sections: list[tuple[Any, list[str]]] = [(None, [])]
                for line in normalized_completion.splitlines():
                    match = field_header_pattern.match(line.strip())
                    if match:
                        header = match.group(1)
                        remaining = line[match.end() :].strip()
                        sections.append((header, [remaining] if remaining else []))
                    else:
                        sections[-1][1].append(line)
                collapsed = [(k, "\n".join(v).strip()) for k, v in sections]
                fields: dict[str, Any] = {}
                recovered_fields: list[str] = []
                for k, v in collapsed:
                    if k in signature.output_fields and k not in fields:
                        annotation = signature.output_fields[k].annotation
                        try:
                            fields[k] = parse_value(v, annotation)
                        except Exception:  # noqa: BLE001 - documented lenient repair of local-model JSON malformations
                            # Recover structural malformations local models produce on
                            # JSON-object fields -- a dropped brace, a constructor-repr,
                            # or a self-named envelope ({"workflow_state": {...}}). All
                            # format-only (no semantic change); see
                            # _recover_malformed_structured_value.
                            try:
                                recovered = _recover_malformed_structured_value(str(k), v)
                            except Exception as recover_exc:
                                _dump_unparseable_completion(
                                    signature,
                                    normalized_completion,
                                    str(k),
                                    v,
                                    str(recover_exc),
                                )
                                raise
                            fields[k] = parse_value(_json.dumps(recovered, default=str), annotation)
                            recovered_fields.append(str(k))
                if fields.keys() != signature.output_fields.keys():
                    raise  # genuinely missing fields — keep the original error
                # Loud trace flag so the semantics are visible: this turn's output
                # was NOT valid for the strict parser and was recovered from a
                # constructor-repr. If you see this a lot, the model isn't emitting
                # JSON natively (a root issue worth fixing upstream, not just here).
                from clio_agent.runtime import trace  # noqa: PLC0415

                trace.event(
                    "LENIENT-ADAPTER RECOVERY",
                    "coerced constructor-repr -> JSON for field(s) %s (strict parse failed: %s)",
                    recovered_fields,
                    str(primary_exc)[:120],
                )
                return fields

        def _clio_resample_attempts(self) -> int:
            return int(getattr(self, "_clio_parse_retry", 0) or 0)

        @staticmethod
        def _clio_trace_resample(attempt: int, exc: Exception) -> None:
            from clio_agent.runtime import trace  # noqa: PLC0415

            trace.event(
                "ADAPTER-RESAMPLE",
                "parse failed (attempt %d), re-sampling the LM call: %s",
                attempt + 1,
                str(exc)[:160],
            )

        def __call__(self, lm, lm_kwargs, signature, demos, inputs):  # type: ignore[no-untyped-def]
            # Bounded re-SAMPLE on an unrecoverable parse failure. The lenient
            # parse() above repairs SHAPE (constructor-repr, dropped brace); it
            # CANNOT recover a genuinely missing field -- e.g. a reasoning model
            # that writes the tool call as prose inside `next_thought` and omits
            # the `next_tool_name`/`next_tool_args` sections entirely. That is a
            # single bad DRAW, not a systematic format: with cache=False at temp>0
            # an independent re-draw almost always emits the full sections. Re-issue
            # the whole call (re-format + re-sample + re-parse) up to N times, then
            # surface the error so the extract-repair / error path still owns it.
            # N is per-model (reasoning models only) -- see create_chat_adapter.
            attempts = self._clio_resample_attempts() + 1
            last_exc: Exception | None = None
            for i in range(attempts):
                try:
                    return super().__call__(lm, lm_kwargs, signature, demos, inputs)
                except AdapterParseError as exc:
                    last_exc = exc
                    if i + 1 < attempts:
                        self._clio_trace_resample(i, exc)
                        continue
                    raise
            assert last_exc is not None
            raise last_exc

        async def acall(self, lm, lm_kwargs, signature, demos, inputs):  # type: ignore[no-untyped-def]
            attempts = self._clio_resample_attempts() + 1
            last_exc: Exception | None = None
            for i in range(attempts):
                try:
                    return await super().acall(lm, lm_kwargs, signature, demos, inputs)
                except AdapterParseError as exc:
                    last_exc = exc
                    if i + 1 < attempts:
                        self._clio_trace_resample(i, exc)
                        continue
                    raise
            assert last_exc is not None
            raise last_exc

    # DSPy's streaming support is gated by an allowlist keyed on the adapter's
    # CLASS NAME STRING (dspy/streaming/streaming_listener.py: it checks
    # ``settings.adapter.__class__.__name__ in {"ChatAdapter","XMLAdapter",
    # "JSONAdapter"}``, NOT isinstance). Our lenient subclass IS a ChatAdapter but
    # its name ("LenientChatAdapter") isn't in that list, so DSPy raises
    # "Unsupported adapter for streaming: LenientChatAdapter" the moment a content
    # chunk streams — which surfaced as nemotron/Sophia's TaskGroup/ExceptionGroup
    # "live streaming failed before emitting output". Report the name as
    # "ChatAdapter" so streaming is accepted; isinstance/behavior are unchanged.
    LenientChatAdapter.__name__ = "ChatAdapter"
    LenientChatAdapter.__qualname__ = "ChatAdapter"

    _LENIENT_CHAT_ADAPTER_CLS = LenientChatAdapter
    return _LENIENT_CHAT_ADAPTER_CLS


def _guided_output_enabled() -> bool:
    """Whether to use guided/structured output (dspy.JSONAdapter) instead of the
    text-protocol ChatAdapter.

    Guided output makes the provider CONSTRAIN generation to the signature's
    output schema (``response_format`` → json_schema when the signature allows,
    else json_object on LM Studio / vLLM), so the structured fields are valid by
    construction instead of relying on the model reproducing the
    ``[[ ## field ## ]]`` text format. This is the reasoning-model fix: qwopus
    drops fields (e.g. ReAct's ``next_tool_name``) under the text protocol; under
    guided output it emits schema-conformant JSON (which, on LM Studio, lands in
    ``reasoning_content`` and is recovered by the content←reasoning_content
    fallback in :meth:`IOLoggingLM._process_completion`).

    Configurable (``lm.guided_output`` / ``CLIO_LM_GUIDED_OUTPUT``), default OFF
    so models that pass on the text protocol (gpt-oss/gemma/nemotron) are
    untouched; opt in per grind / per model.
    """
    try:
        from clio_agent import conf  # noqa: PLC0415

        return bool(
            conf.resolve(
                "lm.guided_output",
                env="CLIO_LM_GUIDED_OUTPUT",
                default=False,
                cast=conf.as_bool,
            )
        )
    except Exception:  # noqa: BLE001 - never let config break adapter construction
        from clio_agent import conf  # noqa: PLC0415

        try:
            return conf.as_bool(os.environ.get("CLIO_LM_GUIDED_OUTPUT", ""))
        except ValueError:
            return False


def _live_streaming_enabled() -> bool:
    """Whether the top-level GACT turn streams the agent's answer live
    (``dspy.streamify`` in :func:`gact.app._try_streamed_forward`) or runs the
    canonical BLOCKING path instead.

    Default ON — unchanged behavior for every model that streams cleanly
    (gpt-oss / gemma / qwopus). The escape hatch exists because some
    reasoning-model + provider combinations stream their answer entirely on the
    ``reasoning_content`` delta channel — which DSPy's content-only stream
    listeners cannot fold into the answer, and which bypasses the
    ``content←reasoning_content`` recovery in
    :meth:`IOLoggingLM._process_completion` (that recovery only runs on the
    blocking path). Symptoms (observed on nvidia/nemotron over ALCF Sophia):
    an empty answer (``stream_completed_without_chunks`` → ``empty_response``)
    or a streamify async ``ExceptionGroup`` ("live streaming failed before
    emitting output"). Disabling live streaming routes such a model through the
    blocking path, where the reasoning channel is recovered and there is no
    streamify task group to fail.

    Configurable (``runtime.live_streaming`` / ``CLIO_LIVE_STREAMING``), default
    ON; opt OUT per grind / per model.
    """
    try:
        from clio_agent import conf  # noqa: PLC0415

        return bool(
            conf.resolve(
                "runtime.live_streaming",
                env="CLIO_LIVE_STREAMING",
                default=True,
                cast=conf.as_bool,
            )
        )
    except Exception:  # noqa: BLE001 - never let config break streaming; default on
        return True


def _reasoning_model_capability(config: LMProviderConfig) -> bool:
    """Per-model: is this a reasoning model (qwopus / qwen3-family ...)?

    Reasoning models route their real output into the ``reasoning_content``
    channel and, under the text protocol, intermittently drop a field on a single
    draw. Two reasoning-only behaviors hang off this flag — the
    ``content<-reasoning_content`` extraction (:meth:`IOLoggingLM._process_completion`)
    and the bounded parse re-sample (the lenient adapter) — so both are applied
    PER MODEL, not globally (today only qwopus/qwen match; others are untouched).

    Override with ``CLIO_LM_REASONING_MODEL`` (1/0); otherwise the per-model
    capability (the handshake ``is_reasoning`` flag, else the name-marker
    detection that reliably identifies qwopus/qwen) decides. This is the interim
    home for what tasks #33/#34 move into the model DB.
    """
    from clio_agent import conf  # noqa: PLC0415 - keep config.py a leaf module
    from clio_agent.config import _uses_local_reasoning_model_profile  # noqa: PLC0415

    # Tri-state: an explicit file/env value forces the flag; absence falls through
    # to the per-model capability detection below.
    raw = conf.resolve("lm.reasoning_model", env="CLIO_LM_REASONING_MODEL", default=None)
    if raw is not None:
        try:
            return conf.as_bool(raw)
        except ValueError:
            pass
    if bool(getattr(config, "is_reasoning", False)):
        return True
    return _uses_local_reasoning_model_profile(config.provider, config.model)


def _parse_retry_attempts(config: LMProviderConfig) -> int:
    """How many times to re-sample the LM on an unrecoverable adapter parse
    failure. Per-model: reasoning models (temp>0, independent re-draws) benefit;
    greedy/non-reasoning models would just repeat the same bad draw, so 0.
    Override with ``CLIO_LM_PARSE_RETRY_ATTEMPTS``."""
    from clio_agent import conf  # noqa: PLC0415 - keep config.py a leaf module

    raw = conf.resolve(
        "limits.lm_parse_retry_attempts", env="CLIO_LM_PARSE_RETRY_ATTEMPTS", default=None
    )
    if raw is not None:
        try:
            return max(0, conf.as_int(raw))
        except (ValueError, TypeError):
            pass
    # The official Codex SDK already returns a complete, expensive reasoning turn.
    # Re-sampling after a formatting error multiplied one visible NDP iteration into
    # three minute-long model calls.  The lenient adapter repairs known format-only
    # shapes; anything still invalid must surface immediately unless an operator has
    # explicitly opted into retries above.
    if str(getattr(config, "provider", "") or "").strip().lower() == "codex":
        return 0
    return 2 if _reasoning_model_capability(config) else 0


def _fix_guided_schema(part: Any) -> None:
    """In-place: pin declared object keys (``additionalProperties=false`` so a
    native-tool-calling model can't substitute its own ``{tool, arguments}``
    shape) while leaving open-ended objects (e.g. ReAct's ``next_tool_args``)
    permissive. Recurses into properties/items/$defs."""
    if not isinstance(part, dict):
        return
    if part.get("type") == "object":
        props = part.get("properties")
        if props:
            part["additionalProperties"] = False
            for sub in props.values():
                _fix_guided_schema(sub)
        else:
            part["additionalProperties"] = True
    if part.get("type") == "array" and isinstance(part.get("items"), dict):
        _fix_guided_schema(part["items"])
    for key in ("$defs", "definitions"):
        for sub in (part.get(key) or {}).values():
            _fix_guided_schema(sub)


def _signature_strict_response_format(signature: Any) -> dict[str, Any]:
    """Build a ``json_schema`` response_format that PINS a DSPy signature's output
    field NAMES (required-as-declared, no extra keys), so a reasoning model that
    natively emits ``{tool, arguments}`` (qwopus) is forced into the requested
    ``{next_thought, next_tool_name, next_tool_args}`` shape.

    Reuses DSPy's pydantic-based schema generation (handles Literal/list/nested),
    but replaces DSPy's open-ended guard+enforce_required (which raises on, or
    over-constrains, ``dict[str, Any]`` leaves) with :func:`_fix_guided_schema`.
    ``strict: false`` because open-ended leaves keep ``additionalProperties:true``
    (incompatible with OpenAI strict mode); LM Studio honors it (verified live).
    """
    import pydantic  # noqa: PLC0415

    fields: dict[str, Any] = {}
    for name, field_info in signature.output_fields.items():
        annotation = field_info.annotation
        default = field_info.default if hasattr(field_info, "default") else ...
        fields[name] = (annotation, default)
    model = pydantic.create_model(
        "ClioGuidedOutputs",
        __config__=pydantic.ConfigDict(extra="forbid"),
        **fields,
    )
    schema = model.model_json_schema()
    for prop in schema.get("properties", {}).values():
        if isinstance(prop, dict):
            prop.pop("json_schema_extra", None)
    _fix_guided_schema(schema)
    return {
        "type": "json_schema",
        "json_schema": {"name": "clio_output", "strict": False, "schema": schema},
    }


_STRICT_GUIDED_ADAPTER_CLS: Any = None


def _strict_guided_json_adapter_cls() -> Any:
    """Build (once) a JSONAdapter subclass that sends a field-name-pinned strict
    json_schema (see :func:`_signature_strict_response_format`) instead of DSPy's
    ``{"type":"json_object"}`` fallback.

    DSPy's JSONAdapter falls back to loose ``json_object`` for any signature with
    an open-ended field, and (a) LM Studio rejects that form, (b) loose lets the
    model emit its native ``{tool, arguments}`` keys -> 0 fields parsed. This
    subclass overrides __call__/acall to set our pinned schema and dispatch via
    ChatAdapter (which uses ``self.parse`` = JSONAdapter's JSON parse). On any
    schema-build failure it defers to stock JSONAdapter behavior.
    """
    global _STRICT_GUIDED_ADAPTER_CLS  # noqa: PLW0603
    if _STRICT_GUIDED_ADAPTER_CLS is not None:
        return _STRICT_GUIDED_ADAPTER_CLS
    dspy = _dspy()

    class StrictGuidedJSONAdapter(dspy.JSONAdapter):  # type: ignore[name-defined]
        def __call__(self, lm, lm_kwargs, signature, demos, inputs):  # type: ignore[no-untyped-def]
            if "response_format" in getattr(lm, "supported_params", []):
                try:
                    kwargs = {
                        **lm_kwargs,
                        "response_format": _signature_strict_response_format(signature),
                    }
                    return dspy.ChatAdapter.__call__(self, lm, kwargs, signature, demos, inputs)
                except Exception as exc:  # noqa: BLE001 - fall back to stock JSONAdapter
                    logger.warning(
                        "strict guided-JSON call failed; degrading to stock JSONAdapter "
                        "reason=strict_response_format_fallback signature=%s error=%s",
                        getattr(signature, "__name__", signature),
                        exc,
                    )
            return dspy.JSONAdapter.__call__(self, lm, lm_kwargs, signature, demos, inputs)

        async def acall(self, lm, lm_kwargs, signature, demos, inputs):  # type: ignore[no-untyped-def]
            if "response_format" in getattr(lm, "supported_params", []):
                try:
                    kwargs = {
                        **lm_kwargs,
                        "response_format": _signature_strict_response_format(signature),
                    }
                    return await dspy.ChatAdapter.acall(self, lm, kwargs, signature, demos, inputs)
                except Exception as exc:  # noqa: BLE001 - fall back to stock JSONAdapter
                    logger.warning(
                        "strict guided-JSON call failed; degrading to stock JSONAdapter "
                        "reason=strict_response_format_fallback signature=%s error=%s",
                        getattr(signature, "__name__", signature),
                        exc,
                    )
            return await dspy.JSONAdapter.acall(self, lm, lm_kwargs, signature, demos, inputs)

    _STRICT_GUIDED_ADAPTER_CLS = StrictGuidedJSONAdapter
    return _STRICT_GUIDED_ADAPTER_CLS


def create_chat_adapter(config: LMProviderConfig) -> Any:
    """Create the DSPy adapter appropriate for this provider.

    Default: ChatAdapter's text protocol (local OpenAI-compatible servers work
    best with it) wrapped in a lenient subclass that, on a structured-output
    parse failure, coerces a constructor-repr field (e.g. qwopus emitting
    ``workflow_state`` as ``Model(field=...)`` instead of JSON) into JSON and
    re-parses — fixing the model↔adapter mismatch in code, no re-request.

    When guided output is enabled (:func:`_guided_output_enabled`), return
    ``dspy.JSONAdapter`` instead: it sends ``response_format`` so the provider
    constrains generation to the output schema — the durable fix for reasoning
    models that drop required fields under the text protocol. LM Studio honors
    ``response_format`` (verified live: it returns schema-conformant JSON, in
    ``reasoning_content``, recovered by the completion fallback); the historical
    "LM Studio rejects response_format with HTTP 400" note no longer holds.

    DSPy's JSON-adapter fallback is kept ONLY for remote providers. On a local
    backend it was historically harmful (the JSON-mode retry's ``response_format``
    once 400'd); local backends rely on the lenient coercion instead.
    ``CLIO_DISABLE_JSON_ADAPTER_FALLBACK`` force-disables it anywhere.
    """
    from clio_agent import conf  # noqa: PLC0415 - keep config.py a leaf module
    from clio_agent.config import is_local_openai_compatible_backend  # noqa: PLC0415

    if _guided_output_enabled():
        return _strict_guided_json_adapter_cls()()
    use_json_fallback = not is_local_openai_compatible_backend(config)

    if conf.resolve(
        "lm.disable_json_adapter_fallback",
        env="CLIO_DISABLE_JSON_ADAPTER_FALLBACK",
        default=False,
        cast=conf.as_bool,
    ):
        use_json_fallback = False
    adapter = _lenient_chat_adapter_cls()(use_json_adapter_fallback=use_json_fallback)
    # Per-model bounded re-sample on an unrecoverable parse failure (reasoning
    # models only; see _parse_retry_attempts). This is the base-case fix for a
    # reasoning model dropping a section (e.g. ReAct's next_tool_name) on one draw.
    adapter._clio_parse_retry = _parse_retry_attempts(config)
    return adapter
