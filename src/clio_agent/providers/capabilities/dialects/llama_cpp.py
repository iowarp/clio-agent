"""llama.cpp dialect adapter (model-capabilities brief Part 6, llama.cpp section).

llama.cpp has no dedicated LiteLLM provider and no official Python client, so
this is a handful of plain HTTP reads mapped onto :class:`DeploymentCapabilities`
/ :class:`EndpointCapabilities` fields. Two server shapes exist:

* **Single-model mode**: one process serves one loaded model. ``GET /props``
  (no query) is always safe to call.
* **Router mode**: one process fronts several model processes. ``GET /models``
  (NOT ``/v1/models``) lists them with ``architecture.input_modalities`` and a
  ``status.args`` array -- the exact CLI flags that model process was launched
  with. ``GET /props?model=<id>`` is only safe to call for an ALREADY LOADED
  model (an unloaded one would be loaded as a side effect of asking).

Every field below is dialect knowledge (which fields llama.cpp's ``/props``
happens to expose, how its router spells a launch flag) -- never a fact about
any one model. ``chat_template_caps`` is stored VERBATIM on
``DeploymentCapabilities.template_caps`` (P4a's :mod:`clio_agent.providers.
capabilities.combine` already reads ``supports_reasoning_effort`` /
``supports_preserve_reasoning`` straight out of it); this module only derives
``tools_enabled`` from it, per the brief ("clio-coder ignores these; use
them").
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from clio_agent.providers.capabilities.link import deployment_model_key_fact
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    EndpointCapabilities,
    Fact,
    ModelCapabilities,
    modalities_from_capabilities,
    unknown,
)

DIALECT = "llama_cpp"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


# --------------------------------------------------------------------------- props (/props)


def fingerprint_from_build_info(build_info: Any) -> str:
    """The endpoint invalidation key (brief 5.6): llama.cpp's ``/props`` ``build_info``.

    A change here (a server restart on a new build) means the SOFTWARE changed,
    so every deployment fact recorded against the old process must be dropped
    too (:func:`clio_agent.providers.capabilities.invalidation.invalidate_endpoint`
    already does that once given this string).
    """
    text = str(build_info or "").strip()
    return f"llama_cpp:build_info={text}" if text else ""


def deployment_fingerprint_from_model_path(model_path: Any) -> str:
    """The deployment invalidation key (brief 5.6): single-model mode's loaded ``model_path``."""
    text = str(model_path or "").strip()
    return f"llama_cpp:model_path={text}" if text else ""


def parse_props(
    payload: Mapping[str, Any],
    *,
    provider_id: str,
    api_base: str,
    model_id: str,
    observed_at: str | None = None,
) -> DeploymentCapabilities:
    """Build one :class:`DeploymentCapabilities` from a ``GET /props`` response.

    Covers ``default_generation_settings.n_ctx`` (served context, per slot),
    ``total_slots``, ``modalities.vision``, ``chat_template_caps`` (verbatim +
    the derived ``tools_enabled``) and ``model_path`` (feeds the model link and
    this deployment's own fingerprint).
    """
    observed_at = observed_at or _now_iso()
    settings = payload.get("default_generation_settings")
    n_ctx = _positive_int(settings.get("n_ctx")) if isinstance(settings, Mapping) else None
    total_slots = _positive_int(payload.get("total_slots"))

    modalities_raw = payload.get("modalities")
    vision = modalities_raw.get("vision") if isinstance(modalities_raw, Mapping) else None
    modalities_known = isinstance(vision, bool)
    modalities_value = frozenset({"text", "image"}) if vision else frozenset({"text"})

    template_caps = payload.get("chat_template_caps")
    template_caps_known = isinstance(template_caps, Mapping)
    caps_dict: dict[str, bool] = (
        {k: bool(v) for k, v in template_caps.items() if isinstance(v, bool)}
        if isinstance(template_caps, Mapping)
        else {}
    )
    tools_enabled = (
        bool(caps_dict.get("supports_tools") or caps_dict.get("supports_tool_calls"))
        if template_caps_known
        else None
    )

    model_path = payload.get("model_path")
    model_key_fact = deployment_model_key_fact(
        model_id,
        observed_at=observed_at,
        gguf_filename=os.path.basename(str(model_path)) if model_path else None,
    )

    return DeploymentCapabilities(
        provider_id=provider_id,
        api_base=api_base,
        model_id=model_id,
        model_key=model_key_fact,
        context_served=(
            Fact(n_ctx, "server_report", observed_at, "llama.cpp /props default_generation_settings.n_ctx")
            if n_ctx is not None
            else unknown()
        ),
        slots=(
            Fact(total_slots, "server_report", observed_at, "llama.cpp /props total_slots")
            if total_slots is not None
            else unknown()
        ),
        modalities_enabled=(
            Fact(modalities_value, "server_report", observed_at, "llama.cpp /props modalities.vision")
            if modalities_known
            else unknown()
        ),
        tools_enabled=(
            Fact(
                tools_enabled,
                "server_report",
                observed_at,
                "llama.cpp /props chat_template_caps.{supports_tools,supports_tool_calls}",
            )
            if tools_enabled is not None
            else unknown()
        ),
        template_caps=(
            Fact(caps_dict, "server_report", observed_at, "llama.cpp /props chat_template_caps")
            if template_caps_known
            else unknown()
        ),
        fingerprint=deployment_fingerprint_from_model_path(model_path),
    )


def parse_v1_models_context_max(payload: Any, model_id: str) -> Fact[int]:
    """``GET /v1/models`` -> the model's own ceiling (brief: ``meta.n_ctx_train``).

    A **model** fact (``source="server_report"``), never a deployment fact --
    this is the model's own published training context, not what any one
    server is currently serving it at.
    """
    rows = payload.get("data") if isinstance(payload, Mapping) else payload
    if not isinstance(rows, list):
        return unknown("llama.cpp /v1/models: unexpected payload shape")
    observed_at = _now_iso()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if model_id and row.get("id") != model_id:
            continue
        meta = row.get("meta")
        n_ctx_train = _positive_int(meta.get("n_ctx_train")) if isinstance(meta, Mapping) else None
        if n_ctx_train is not None:
            return Fact(n_ctx_train, "server_report", observed_at, "llama.cpp /v1/models meta.n_ctx_train")
    return unknown("llama.cpp /v1/models: no matching row or no meta.n_ctx_train")


def build_model_capabilities(model_key: str, v1_models_payload: Any, model_id: str) -> ModelCapabilities:
    """The model-record side of ``GET /v1/models`` (brief: ``meta.n_ctx_train``)."""
    return ModelCapabilities(model_key=model_key, context_max=parse_v1_models_context_max(v1_models_payload, model_id))


# --------------------------------------------------------------------------- fetch (plain HTTP)


async def fetch_props(client: Any, root: str, *, model_id: str = "", loaded: bool = True) -> dict[str, Any] | None:
    """``GET /props`` (single mode, no query) or ``GET /props?model=<id>`` (router mode).

    The router-mode query form is only safe for an ALREADY LOADED model (brief:
    asking for an unloaded one loads it as a side effect), so callers pass
    ``model_id`` there only when they already know ``loaded=True``; the default
    (no ``model_id``) is always the safe, unqualified single-mode form.
    """
    url = f"{root}/props"
    if model_id and loaded:
        url = f"{root}/props?model={model_id}"
    try:
        response = await client.get(url)
        if response.status_code >= 400:
            return None
        payload = response.json()
    except Exception:  # noqa: BLE001 - /props is best-effort; the enrich cascade fills the gap
        return None
    return payload if isinstance(payload, dict) else None


async def fetch_v1_models(client: Any, root: str) -> Any:
    """``GET /v1/models`` -> the raw payload (brief: ``meta.n_ctx_train``), or ``None``."""
    try:
        response = await client.get(f"{root}/v1/models")
        if response.status_code >= 400:
            return None
        return response.json()
    except Exception:  # noqa: BLE001 - best-effort; context_max stays unknown
        return None


async def fetch_router_models(client: Any, root: str) -> Any:
    """Router mode ``GET /models`` -> the raw payload, or ``None``."""
    try:
        response = await client.get(f"{root}/models")
        if response.status_code >= 400:
            return None
        return response.json()
    except Exception:  # noqa: BLE001 - best-effort; no router rows discovered
        return None


# --------------------------------------------------------------------------- router mode (/models)


@dataclass(frozen=True)
class RouterServerFlags:
    """Parsed ``status.args`` for one router-mode model process (brief Part 6).

    Attributes mirror the exact CLI flags llama.cpp's router reports having
    launched that model process with. Ported from clio-coder's
    ``parseLlamaCppServerFlags`` (``common/probe-helpers.ts``) plus the
    ``--kv-unified`` served-context rule the brief adds (clio-coder does not
    implement that one -- "clio-coder ignores these; use them").
    """

    ctx_size: int | None = None
    parallel: int | None = None
    kv_unified: bool = False
    jinja: bool = False
    mmproj: str | None = None
    reasoning: str | None = None
    reasoning_budget: int | None = None
    chat_template_kwargs: dict[str, Any] | None = None


def _value_after(args: Sequence[str], flag: str) -> str | None:
    try:
        index = args.index(flag)
    except ValueError:
        return None
    if index + 1 >= len(args):
        return None
    value = args[index + 1]
    return value if not value.startswith("--") else None


def _bool_flag(args: Sequence[str], flag: str) -> bool:
    return flag in args


def _int_value(args: Sequence[str], flag: str) -> int | None:
    value = _value_after(args, flag)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_status_args(args: Sequence[str] | str | None) -> RouterServerFlags:
    """Parse one router model row's ``status.args`` into :class:`RouterServerFlags`.

    ``args`` may already be a list (the common shape) or a single
    whitespace-joined command-line string (defensive: some builds report it
    that way) -- both normalize the same way clio-coder's ``argsFromStatus``
    does.
    """
    if isinstance(args, str):
        tokens = args.split()
    elif isinstance(args, Sequence):
        tokens = [str(a) for a in args]
    else:
        tokens = []

    kwargs_raw = _value_after(tokens, "--chat-template-kwargs")
    chat_template_kwargs: dict[str, Any] | None = None
    if kwargs_raw:
        import json  # noqa: PLC0415

        try:
            parsed = json.loads(kwargs_raw)
            chat_template_kwargs = parsed if isinstance(parsed, dict) else {"raw": kwargs_raw}
        except ValueError:
            chat_template_kwargs = {"raw": kwargs_raw}

    return RouterServerFlags(
        ctx_size=_int_value(tokens, "--ctx-size"),
        parallel=_int_value(tokens, "--parallel"),
        kv_unified=_bool_flag(tokens, "--kv-unified"),
        jinja=_bool_flag(tokens, "--jinja"),
        mmproj=_value_after(tokens, "--mmproj"),
        reasoning=_value_after(tokens, "--reasoning"),
        reasoning_budget=_int_value(tokens, "--reasoning-budget"),
        chat_template_kwargs=chat_template_kwargs,
    )


def served_context_from_flags(flags: RouterServerFlags) -> int | None:
    """Served context per slot (brief): ``--ctx-size / --parallel``, unless ``--kv-unified``.

    ``--kv-unified`` means the KV cache is one unified pool rather than sliced
    per parallel slot, so the FULL ``--ctx-size`` is what is actually served
    (no division). Missing ``--parallel`` defaults to 1 (llama.cpp's own
    default), which makes the division a no-op when parallelism was not set.
    """
    if flags.ctx_size is None:
        return None
    if flags.kv_unified:
        return flags.ctx_size
    parallel = flags.parallel or 1
    if parallel <= 0:
        return flags.ctx_size
    return flags.ctx_size // parallel


def deployment_fingerprint_from_args(args: Sequence[str] | str | None) -> str:
    """The router-mode deployment invalidation key (brief 5.6): the status args."""
    if isinstance(args, str):
        text = args
    elif isinstance(args, Sequence):
        text = " ".join(str(a) for a in args)
    else:
        text = ""
    if not text.strip():
        return ""
    return f"llama_cpp:status_args_sha256={sha256(text.encode('utf-8')).hexdigest()[:16]}"


def parse_router_model_row(
    row: Mapping[str, Any],
    *,
    provider_id: str,
    api_base: str,
    observed_at: str | None = None,
) -> DeploymentCapabilities:
    """Build one router-mode model's :class:`DeploymentCapabilities` from a ``GET /models`` row.

    Reads ``architecture.input_modalities`` and parses ``status.args`` (brief:
    served context, ``--jinja`` -> tools, ``--mmproj`` -> vision, ``--reasoning``/
    ``--reasoning-budget`` -> a controllable thinking mechanism, and
    ``--chat-template-kwargs`` -> the default template kwargs).
    """
    observed_at = observed_at or _now_iso()
    model_id = str(row.get("id") or "")
    architecture = row.get("architecture")
    input_modalities = (
        architecture.get("input_modalities") if isinstance(architecture, Mapping) else None
    )
    modalities_known = isinstance(input_modalities, list)
    modalities_value = modalities_from_capabilities(input_modalities) if modalities_known else None

    status = row.get("status")
    args = status.get("args") if isinstance(status, Mapping) else None
    flags = parse_status_args(args)
    served_context = served_context_from_flags(flags)
    vision_from_flags = bool(flags.mmproj)
    if vision_from_flags and modalities_value is not None:
        modalities_value = modalities_value | {"image"}
    elif vision_from_flags:
        modalities_value = frozenset({"text", "image"})
        modalities_known = True

    reasoning_enabled = bool(flags.reasoning) or flags.reasoning_budget is not None

    model_key_fact = deployment_model_key_fact(model_id, observed_at=observed_at)

    return DeploymentCapabilities(
        provider_id=provider_id,
        api_base=api_base,
        model_id=model_id,
        model_key=model_key_fact,
        context_served=(
            Fact(served_context, "server_report", observed_at, "llama.cpp router status.args --ctx-size/--parallel")
            if served_context is not None
            else unknown()
        ),
        modalities_enabled=(
            Fact(modalities_value, "server_report", observed_at, "llama.cpp router architecture.input_modalities/--mmproj")
            if modalities_known
            else unknown()
        ),
        tools_enabled=Fact(
            flags.jinja, "server_report", observed_at, "llama.cpp router status.args --jinja"
        ),
        reasoning_enabled=Fact(
            reasoning_enabled,
            "server_report",
            observed_at,
            "llama.cpp router status.args --reasoning/--reasoning-budget",
        ),
        default_template_kwargs=(
            Fact(
                flags.chat_template_kwargs,
                "server_report",
                observed_at,
                "llama.cpp router status.args --chat-template-kwargs",
            )
            if flags.chat_template_kwargs is not None
            else unknown()
        ),
        fingerprint=deployment_fingerprint_from_args(args),
    )


def parse_router_models(
    payload: Any, *, provider_id: str, api_base: str
) -> dict[str, DeploymentCapabilities]:
    """Parse a full router-mode ``GET /models`` payload into one row per model id."""
    rows = payload.get("models") if isinstance(payload, Mapping) else payload
    if not isinstance(rows, list):
        return {}
    observed_at = _now_iso()
    out: dict[str, DeploymentCapabilities] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        deployment = parse_router_model_row(
            row, provider_id=provider_id, api_base=api_base, observed_at=observed_at
        )
        if deployment.model_id:
            out[deployment.model_id] = deployment
    return out


# --------------------------------------------------------------------------- endpoint


def build_endpoint_capabilities(
    provider_id: str, api_base: str, model_id: str, *, build_info: Any, multi_model: bool = False
) -> EndpointCapabilities:
    """Build this endpoint's :class:`EndpointCapabilities` (brief 5.2 + 5.6).

    Delegates the accepted-parameter / thinking-control resolution to
    :func:`clio_agent.providers.capabilities.endpoint.build_endpoint_capabilities`
    (the shared LiteLLM + supplement-table logic every dialect uses); this
    function's own job is only to plug in llama.cpp's own fingerprint/version
    (``build_info``), which no generic code can know.
    """
    from clio_agent.providers.capabilities import endpoint as capability_endpoint  # noqa: PLC0415

    observed_at = _now_iso()
    build_info_text = str(build_info or "").strip()
    return capability_endpoint.build_endpoint_capabilities(
        provider_id,
        api_base,
        DIALECT,
        model_id,
        custom_llm_provider="openai",  # llama.cpp has no dedicated litellm provider
        multi_model=multi_model,
        server_version=(
            Fact(build_info_text, "server_report", observed_at, "llama.cpp /props build_info")
            if build_info_text
            else None
        ),
        fingerprint=fingerprint_from_build_info(build_info),
    )


__all__ = [
    "DIALECT",
    "RouterServerFlags",
    "build_endpoint_capabilities",
    "build_model_capabilities",
    "deployment_fingerprint_from_args",
    "deployment_fingerprint_from_model_path",
    "fetch_props",
    "fetch_router_models",
    "fetch_v1_models",
    "fingerprint_from_build_info",
    "parse_props",
    "parse_router_model_row",
    "parse_router_models",
    "parse_status_args",
    "parse_v1_models_context_max",
    "served_context_from_flags",
]
