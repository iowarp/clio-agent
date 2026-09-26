"""Ollama dialect adapter (model-capabilities brief Part 6, Ollama section).

The SINGLE place that reads Ollama's native REST surface and maps it onto the
capability records -- :class:`~clio_agent.providers.handshake.ollama.
OllamaHandshake` calls this module for every HTTP read and does no parsing of
its own (consolidation review of #1447: two readers of ``/api/show``/``/api/ps``
existed briefly, one here and one hand-rolled in the handshake class; this
module now owns all of it).

Reads plain HTTP verbs (``client.get``/``client.post``) through whatever
client :class:`~clio_agent.providers.handshake.base.ProviderHandshake` opened
-- the SAME ``httpx.AsyncClient`` every other dialect adapter uses, not the
official ``ollama`` Python package's client. That package's ``AsyncClient``
has no generic ``get``/``post`` (only typed methods like ``show``/``ps``), so
using it here would force a SECOND, incompatible client type through the one
``ProviderHandshake.handshake()`` loop that already threads a single
``httpx.AsyncClient`` across connectivity, discovery and per-model config --
exactly the "two ways to reach the same endpoint" duplication this
consolidation removes elsewhere. Ollama's native API is plain JSON-over-HTTP,
so nothing is lost by reading it the same way every other dialect does.

Covers, all as brief-Part-6 facts:

* ``GET /api/tags`` -- installed models (:func:`fetch_tags`).
* ``POST /api/show`` -- [M] ``model_info.<arch>.context_length``,
  ``capabilities`` (tools/thinking/vision), [D] the Modelfile ``parameters``
  blob (``num_ctx`` and sampling defaults).
* ``GET /api/ps`` -- [D] the context ACTUALLY loaded right now;
  ``context_served`` is the smaller of that and the Modelfile's ``num_ctx``
  (clio-coder ``local-native/ollama.ts``).
* ``GET /api/version`` -- [E] the endpoint's own fingerprint (brief 5.6).

Model linking's third named case ("Ollama's ``hf.co/<repo>`` names") is
already handled generically by :mod:`clio_agent.providers.capabilities.link`
(``_HF_CO_NAME``) -- nothing dialect-specific to add here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.capabilities.link import deployment_model_key_fact
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    EndpointCapabilities,
    Fact,
    ModelCapabilities,
    ThinkingSpec,
    modalities_from_capabilities,
    task_fact,
    unknown,
)

DIALECT = "ollama"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


# --------------------------------------------------------------------------- fetch (plain HTTP)


async def fetch_tags(client: Any, root: str) -> list[dict[str, Any]]:
    """``GET /api/tags`` -> normalized ``{"id", ...}`` rows for installed models."""
    try:
        response = await client.get(f"{root}/api/tags")
        if response.status_code >= 400:
            return []
        payload = response.json()
    except Exception:  # noqa: BLE001 - an unparseable/unreachable tags listing yields no rows
        return []
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    rows: list[dict[str, Any]] = []
    for entry in models:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("model") or entry.get("name")
        if model_id:
            rows.append({"id": model_id, **entry})
    return rows


async def fetch_show(client: Any, root: str, model_id: str) -> dict[str, Any] | None:
    """``POST /api/show {"model": id}`` -> the raw per-model metadata, or ``None``."""
    try:
        response = await client.post(f"{root}/api/show", json={"model": model_id})
        if response.status_code >= 400:
            return None
        data = response.json()
    except Exception:  # noqa: BLE001 - /api/show is best-effort; the enrich cascade fills the gap
        return None
    return data if isinstance(data, dict) else None


async def fetch_ps(client: Any, root: str) -> dict[str, Any] | None:
    """``GET /api/ps`` -> the raw resident-model listing, or ``None``."""
    try:
        response = await client.get(f"{root}/api/ps")
        if response.status_code >= 400:
            return None
        data = response.json()
    except Exception:  # noqa: BLE001 - /api/ps is best-effort (older server, transient error)
        return None
    return data if isinstance(data, dict) else None


async def fetch_version(client: Any, root: str) -> str | None:
    """``GET /api/version`` -> ``{"version": "..."}``, or ``None``.

    Best-effort: a failure (older server, transient error) yields ``None``,
    never raises -- this is enrichment, not a connectivity gate.
    """
    try:
        response = await client.get(f"{root}/api/version")
        if response.status_code >= 400:
            return None
        payload = response.json()
    except Exception:  # noqa: BLE001 - best-effort enrichment, never sinks discovery
        return None
    version = payload.get("version") if isinstance(payload, dict) else None
    return str(version) if version else None


# --------------------------------------------------------------------------- parse (pure)


def parse_modelfile_parameters(raw: str | None) -> dict[str, Any]:
    """Parse Ollama's ``/api/show`` ``parameters`` blob into a ``{key: value}`` dict.

    The field is plain Modelfile ``PARAMETER`` text, one ``key value`` pair per
    line (e.g. ``"num_ctx 8192\\ntemperature 0.7\\nstop \\"<|im_end|>\\""``), never
    JSON. A value that parses as an int or float is normalized to that type
    (``num_ctx``, sampling knobs); anything else is kept as a stripped string.
    A repeated key (``stop`` is commonly set multiple times, once per sequence)
    is collected into a list rather than the later line silently overwriting
    the earlier one.
    """
    if not raw:
        return {}
    out: dict[str, Any] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        key, value_text = parts
        value_text = value_text.strip().strip('"')
        value: Any = value_text
        try:
            value = int(value_text)
        except ValueError:
            try:
                value = float(value_text)
            except ValueError:
                pass
        if key in out:
            existing = out[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                out[key] = [existing, value]
        else:
            out[key] = value
    return out


def fingerprint_from_version(version: Any) -> str:
    """The endpoint invalidation key (brief 5.6): ``GET /api/version``."""
    text = str(version or "").strip()
    return f"ollama:version={text}" if text else ""


def deployment_fingerprint(digest: Any, loaded_context: int | None) -> str:
    """The deployment invalidation key (brief 5.6): model ``digest`` + loaded context."""
    digest_text = str(digest or "").strip()
    if not digest_text and loaded_context is None:
        return ""
    return f"ollama:digest={digest_text}:loaded_context={loaded_context}"


def loaded_context_from_ps(payload: Any, model_id: str) -> int | None:
    """The context actually loaded right now, from a raw ``GET /api/ps`` JSON payload."""
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return None
    for row in models:
        if not isinstance(row, dict):
            continue
        wire_id = row.get("model") or row.get("name")
        if wire_id != model_id:
            continue
        return _positive_int(row.get("context_length"))
    return None


def digest_from_ps(payload: Any, model_id: str) -> str | None:
    """The loaded model's ``digest`` from a raw ``GET /api/ps`` JSON payload, or ``None``."""
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return None
    for row in models:
        if not isinstance(row, dict):
            continue
        wire_id = row.get("model") or row.get("name")
        if wire_id == model_id:
            digest = row.get("digest")
            return str(digest) if digest else None
    return None


def show_identity(data: Any) -> tuple[str | None, tuple[str, ...]]:
    """``(arch, capabilities)`` from a raw ``/api/show`` payload -- shared by :func:`parse_show`
    and the handshake's raw passthrough metadata, so the field names
    (``model_info.general.architecture``, ``capabilities``) are written once.
    """
    if not isinstance(data, dict):
        return None, ()
    arch: str | None = None
    info = data.get("model_info")
    if isinstance(info, dict):
        raw_arch = info.get("general.architecture")
        arch = raw_arch if isinstance(raw_arch, str) and raw_arch else None
    raw_caps = data.get("capabilities")
    caps = tuple(str(c) for c in raw_caps) if isinstance(raw_caps, list) else ()
    return arch, caps


def parse_show(data: Any, *, model_key: str, observed_at: str | None = None) -> ModelCapabilities:
    """Build the MODEL half of ``POST /api/show`` (brief Part 6 Ollama section).

    ``model_info.<arch>.context_length`` -> the model's own ceiling;
    ``capabilities`` is an exhaustive, self-reported list, so an ABSENT entry
    (``"tools"``, ``"thinking"``) is real negative evidence, not "unknown".
    """
    observed_at = observed_at or _now_iso()
    if not isinstance(data, dict):
        return ModelCapabilities(model_key=model_key)

    arch, caps = show_identity(data)
    context_window: int | None = None
    info = data.get("model_info")
    if arch and isinstance(info, dict):
        context_window = _positive_int(info.get(f"{arch}.context_length"))
    capabilities_known = bool(caps)
    # The exhaustive capabilities list names the task: ``embedding`` models
    # embed; ``completion`` models generate text.
    task = (
        "feature-extraction"
        if "embedding" in caps
        else "text-generation"
        if "completion" in caps
        else None
    )

    return ModelCapabilities(
        model_key=model_key,
        task=task_fact(
            task, source="server_report", observed_at=observed_at, detail="ollama /api/show capabilities"
        ),
        context_max=(
            Fact(context_window, "server_report", observed_at, "ollama /api/show model_info.<arch>.context_length")
            if context_window is not None
            else unknown()
        ),
        tools=(
            Fact(value="tools" in caps, source="server_report", observed_at=observed_at, detail="ollama /api/show capabilities")
            if capabilities_known
            else unknown()
        ),
        input_modalities=(
            Fact(
                value=modalities_from_capabilities(caps),
                source="server_report",
                observed_at=observed_at,
                detail="ollama /api/show capabilities",
            )
            if capabilities_known
            else unknown()
        ),
        thinking=(
            Fact(
                value=ThinkingSpec(mechanism="on_off" if "thinking" in caps else "none"),
                source="server_report",
                observed_at=observed_at,
                detail="ollama /api/show capabilities",
            )
            if capabilities_known
            else unknown()
        ),
    )


def build_deployment_extra(
    *,
    provider_id: str,
    api_base: str,
    model_id: str,
    show_parameters: str | None,
    ps_payload: Any,
) -> DeploymentCapabilities:
    """The DEPLOYMENT half of ``/api/show`` + ``/api/ps`` (brief Part 6 Ollama section).

    ``context_served`` is the smaller of the Modelfile's ``num_ctx`` and the
    context actually loaded per ``/api/ps`` (clio-coder ``local-native/ollama.ts``);
    when only one is known, that one stands (never treated as a hard ceiling
    from the other side).
    """
    observed_at = _now_iso()
    params = parse_modelfile_parameters(show_parameters)
    num_ctx = _positive_int(params.get("num_ctx"))
    loaded_ctx = loaded_context_from_ps(ps_payload, model_id)
    candidates = [c for c in (num_ctx, loaded_ctx) if c is not None]
    context_served = min(candidates) if candidates else None
    digest = digest_from_ps(ps_payload, model_id)

    model_key_fact = deployment_model_key_fact(model_id, observed_at=observed_at)

    return DeploymentCapabilities(
        provider_id=provider_id,
        api_base=api_base,
        model_id=model_id,
        model_key=model_key_fact,
        context_served=(
            Fact(
                context_served,
                "server_report",
                observed_at,
                "ollama min(/api/show parameters.num_ctx, /api/ps context_length)",
            )
            if context_served is not None
            else unknown()
        ),
        fingerprint=deployment_fingerprint(digest, loaded_ctx),
    )


def build_endpoint_capabilities(
    provider_id: str, api_base: str, model_id: str, *, version: Any
) -> EndpointCapabilities:
    """Build this endpoint's :class:`EndpointCapabilities`, plugging in ``GET /api/version``."""
    from clio_agent.providers.capabilities import endpoint as capability_endpoint  # noqa: PLC0415

    observed_at = _now_iso()
    version_text = str(version or "").strip()
    return capability_endpoint.build_endpoint_capabilities(
        provider_id,
        api_base,
        DIALECT,
        model_id,
        custom_llm_provider="ollama_chat",
        server_version=(
            Fact(version_text, "server_report", observed_at, "ollama /api/version")
            if version_text
            else None
        ),
        fingerprint=fingerprint_from_version(version),
    )


__all__ = [
    "DIALECT",
    "build_deployment_extra",
    "build_endpoint_capabilities",
    "deployment_fingerprint",
    "digest_from_ps",
    "fetch_ps",
    "fetch_show",
    "fetch_tags",
    "fetch_version",
    "fingerprint_from_version",
    "loaded_context_from_ps",
    "parse_modelfile_parameters",
    "parse_show",
    "show_identity",
]
