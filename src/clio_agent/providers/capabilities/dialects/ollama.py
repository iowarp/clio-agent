"""Ollama dialect adapter (model-capabilities brief Part 6, Ollama section).

Uses the official ``ollama`` Python client (added as a proper dependency this
slice -- it was not one before) for ``show``/``ps``, since both are thin,
already-typed wrappers over Ollama's native REST API; ``/api/version`` has no
client method, so it is one plain GET through the client's own underlying
``httpx.AsyncClient`` (``ollama.AsyncClient._client``) rather than a second,
hand-rolled HTTP client.

P4a's :mod:`clio_agent.providers.handshake.ollama` already reads ``/api/show``
for the MODEL facts (``capabilities``, ``model_info.<arch>.context_length``).
This module adds what that handshake does not cover, all of it DEPLOYMENT
evidence (brief Part 4):

* ``/api/show`` ``parameters`` -- the Modelfile defaults (``num_ctx`` and
  sampling), a plain multi-line ``key value`` text blob, not JSON.
* ``/api/ps`` -- the context ACTUALLY loaded right now; ``context_served`` is
  the smaller of that and the Modelfile's ``num_ctx`` (clio-coder
  ``local-native/ollama.ts``).
* ``/api/version`` -- the endpoint's own fingerprint (brief 5.6).

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
    unknown,
)

DIALECT = "ollama"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


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


def build_deployment_extra(
    *,
    provider_id: str,
    api_base: str,
    model_id: str,
    show_parameters: str | None,
    ps_payload: Any,
) -> DeploymentCapabilities:
    """The DEPLOYMENT facts this module adds on top of P4a's ``/api/show`` model facts.

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


def _model_dump(obj: Any) -> dict[str, Any]:
    """Normalize one ``ollama`` client response object (or a plain dict) to a dict."""
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dict(dump())
    return dict(obj)


async def fetch_deployment_facts(
    client: Any, *, provider_id: str, api_base: str, model_id: str
) -> DeploymentCapabilities:
    """Fetch ``/api/show`` + ``/api/ps`` through the official ``ollama.AsyncClient`` and build the deployment record.

    ``client`` is an ``ollama.AsyncClient`` (typed ``Any`` here to keep this
    module importable without the dependency at type-check time for callers
    that don't need it). Each read is independently best-effort -- either can
    be absent (older server, transient error, the model just isn't currently
    loaded so ``/api/ps`` omits it) without failing the other.
    """
    show_parameters: str | None = None
    ps_payload: dict[str, Any] | None = None
    try:
        show = await client.show(model_id)
        show_parameters = getattr(show, "parameters", None)
    except Exception:  # noqa: BLE001 - best-effort enrichment, never sinks discovery
        pass
    try:
        ps = await client.ps()
        models = getattr(ps, "models", None) or []
        ps_payload = {"models": [_model_dump(m) for m in models]}
    except Exception:  # noqa: BLE001 - best-effort enrichment, never sinks discovery
        pass
    return build_deployment_extra(
        provider_id=provider_id,
        api_base=api_base,
        model_id=model_id,
        show_parameters=show_parameters,
        ps_payload=ps_payload,
    )


async def fetch_version(client: Any, api_base: str) -> str | None:
    """``GET /api/version`` -> ``{"version": "..."}``.

    No ``ollama`` client method covers this, so it is one plain GET through the
    SAME httpx transport the client itself uses (``client._client``) rather
    than opening a second HTTP client. Best-effort: a failure (older server,
    transient error) yields ``None``, never raises -- this is enrichment, not a
    connectivity gate.
    """
    from clio_agent.providers.api_base import native_root  # noqa: PLC0415

    root = native_root(api_base)
    try:
        response = await client._client.get(f"{root}/api/version")  # noqa: SLF001 - see docstring
        if response.status_code >= 400:
            return None
        payload = response.json()
    except Exception:  # noqa: BLE001 - best-effort enrichment, never sinks discovery
        return None
    version = payload.get("version") if isinstance(payload, dict) else None
    return str(version) if version else None


__all__ = [
    "DIALECT",
    "build_deployment_extra",
    "build_endpoint_capabilities",
    "deployment_fingerprint",
    "digest_from_ps",
    "fetch_deployment_facts",
    "fetch_version",
    "fingerprint_from_version",
    "loaded_context_from_ps",
    "parse_modelfile_parameters",
]
