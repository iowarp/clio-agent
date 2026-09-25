"""Link a deployment's wire model id to a model record (brief Part 5.4).

The server's own wire id (``local-model``, ``qwen3:8b``, an OpenRouter slug)
means nothing to the model-record cache on its own -- it has to be resolved to
a :data:`~clio_agent.providers.identity.ModelKey` so
:class:`~clio_agent.providers.capabilities.records.DeploymentCapabilities` and
:class:`~clio_agent.providers.capabilities.records.ModelCapabilities` can be
joined. The brief gives an ordered, closed set of rules and is explicit that
NONE of them may guess from a partial name -- a rule either matches exactly or
it doesn't, and :func:`link_model` always reports which one fired (or that
none did) so the decision is auditable rather than implied.

Only the first two rule FAMILIES are implemented as real logic here:

1. an exact cloud model id the community catalogs already know (``known_cloud_id``,
   injected so this module stays free of network/catalog-fetch code -- callers
   pass a plain predicate backed by whatever catalog they trust);
2. the three Hugging Face repo shapes the brief names: a vLLM ``root`` field
   (self-reported, so it is passed straight through), an Ollama ``hf.co/<repo>``
   pulled-model name, and a llama.cpp router id of the form
   ``org/repo-GGUF[:quant]``.

The third rule (an overlay ``matchPatterns`` entry, P6/brief Part 8) is now
wired: ``overlay_match`` is the real
:func:`clio_agent.providers.capabilities.model_overlay.overlay_match_for_link`
at every call site. Per the brief ("matches against the wire id, the GGUF
filename and the Ollama model name"), the rule tries every DISTINCT candidate
string a caller supplies -- ``wire_id`` first, then ``gguf_filename`` when it
differs -- so a wire id an endpoint spells generically (llama.cpp router mode's
``local-model``) can still match a family pattern that only appears in the
loaded GGUF's filename. Ollama's own wire id already IS "the Ollama model
name" (:mod:`...handshake.ollama` passes ``/api/tags``' ``model``/``name``
straight through), so no separate parameter exists for it. Passing
``overlay_match=None`` (still the default for a caller with none, e.g. a unit
test) makes the rule a no-op, which is exactly "no overlay to consult", not
"the overlay found nothing".
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from clio_agent.providers.capabilities.records import Fact

#: Ollama names a pulled Hugging Face GGUF as ``hf.co/<org>/<repo>[:tag]``.
_HF_CO_NAME = re.compile(r"^hf\.co/(?P<repo>[^:]+)", re.IGNORECASE)

#: llama.cpp router ids for a GGUF repo: ``org/repo-GGUF`` or ``org/repo-GGUF:quant``.
_LLAMA_CPP_ROUTER_GGUF = re.compile(
    r"^(?P<repo>[^/\s]+/[^/\s]+?)-GGUF(?::[^/\s]+)?$", re.IGNORECASE
)

#: The rule that decided a link, in the brief's own order. ``"no_link"`` is a
#: real outcome (brief 5.4 step 4), not a failure -- the deployment still works
#: on its own facts.
LinkRule = str


@dataclass(frozen=True)
class LinkResult:
    """The outcome of :func:`link_model`: the resolved key, if any, and why.

    Attributes:
        model_key: The resolved :data:`~clio_agent.providers.identity.ModelKey`,
            or ``None`` for "no link".
        rule: Which rule matched: ``"cloud_catalog"``, ``"hf_vllm_root"``,
            ``"hf_ollama_pull"``, ``"hf_llama_cpp_router"``, ``"overlay_match"``,
            or ``"no_link"``.
        detail: A short human-readable trace of the match.
    """

    model_key: str | None
    rule: LinkRule
    detail: str = ""


def link_model(
    wire_id: str,
    *,
    known_cloud_id: Callable[[str], bool] | None = None,
    vllm_root: str | None = None,
    gguf_filename: str | None = None,
    overlay_match: Callable[[str], str | None] | None = None,
) -> LinkResult:
    """Resolve one server wire id to a model key, trying each rule in order.

    Args:
        wire_id: The id the server itself uses (``DeploymentCapabilities.model_id``).
        known_cloud_id: Predicate answering "the community catalogs know this
            EXACT id" for a cloud dialect. Never called with a guessed/partial
            id -- only ``wire_id`` itself.
        vllm_root: vLLM's self-reported ``/v1/models`` ``root`` field for this
            model, when the adapter has it. Already a Hugging Face repo id by
            vLLM's own convention, so it is trusted directly.
        gguf_filename: The GGUF filename from llama.cpp's ``/props``
            ``model_path`` (basename, no directories), when available -- tried
            against the router-id shape in addition to ``wire_id`` itself, and
            also offered to ``overlay_match`` (rule 3) when it differs from
            ``wire_id``.
        overlay_match: The overlay's ``matchPatterns`` lookup (brief Part 8 /
            P6): given one candidate string, returns the matching family's
            ``model_key``, or ``None`` for no match. ``None`` (the default)
            means no overlay was wired for this call, which is distinct from
            "the overlay ran and found nothing" (that would return ``None``
            from a *call*, not from omitting the callable).

    Returns:
        A :class:`LinkResult` naming which rule matched, or ``"no_link"``.
    """

    cleaned = (wire_id or "").strip()
    if not cleaned:
        return LinkResult(None, "no_link", "empty wire id")

    if known_cloud_id is not None:
        try:
            is_known = known_cloud_id(cleaned)
        except Exception:  # noqa: BLE001 - a broken catalog predicate must not break linking
            is_known = False
        if is_known:
            return LinkResult(cleaned, "cloud_catalog", f"{cleaned!r} is a known cloud model id")

    if vllm_root:
        return LinkResult(
            vllm_root, "hf_vllm_root", f"vLLM /v1/models root={vllm_root!r} (self-reported)"
        )

    match = _HF_CO_NAME.match(cleaned)
    if match:
        repo = match.group("repo")
        return LinkResult(repo, "hf_ollama_pull", f"ollama hf.co name -> repo={repo!r}")

    for candidate in (cleaned, gguf_filename or ""):
        if not candidate:
            continue
        match = _LLAMA_CPP_ROUTER_GGUF.match(candidate)
        if match:
            repo = match.group("repo")
            return LinkResult(
                repo, "hf_llama_cpp_router", f"llama.cpp router id {candidate!r} -> repo={repo!r}"
            )

    if overlay_match is not None:
        candidates = [cleaned]
        if gguf_filename and gguf_filename != cleaned:
            candidates.append(gguf_filename)
        for candidate in candidates:
            try:
                matched = overlay_match(candidate)
            except Exception:  # noqa: BLE001 - a broken overlay lookup must not break linking
                matched = None
            if matched:
                return LinkResult(
                    matched,
                    "overlay_match",
                    f"overlay matchPatterns -> {matched!r} (candidate={candidate!r})",
                )

    return LinkResult(None, "no_link", f"no rule matched {cleaned!r}")


def deployment_model_key_fact(
    wire_id: str, *, observed_at: str, **link_kwargs: object
) -> Fact[str]:
    """Resolve ``wire_id`` to a :class:`DeploymentCapabilities.model_key` fact.

    Runs :func:`link_model` and, when no rule matches, falls back to the wire
    id itself as a per-deployment pseudo key -- this is what lets a
    :class:`~clio_agent.providers.capabilities.records.ModelCapabilities`
    record (keyed the same way by an adapter with nothing better to key it by)
    and this deployment's record actually join in
    :func:`clio_agent.providers.capabilities.accessor.get_effective_capabilities`,
    even before a real Hugging Face/overlay/cloud-catalog link exists. The
    fallback is never presented as a real link: its ``detail`` says plainly
    that no rule matched.
    """

    result = link_model(wire_id, **link_kwargs)  # type: ignore[arg-type]
    if result.model_key:
        return Fact(
            value=result.model_key,
            source="server_report",
            observed_at=observed_at,
            detail=f"link rule={result.rule}: {result.detail}",
        )
    return Fact(
        value=wire_id,
        source="server_report",
        observed_at=observed_at,
        detail="no link rule matched; using the wire id as a per-deployment model_key",
    )


__all__ = ["LinkResult", "LinkRule", "deployment_model_key_fact", "link_model"]
