"""The Hugging Face repo layer (model-capabilities brief Part 6.1), filling P4a's ``HfRepoSource``.

Feeds ONLY :class:`~clio_agent.providers.capabilities.records.ModelCapabilities`
(brief: model 5.1 step 4) -- recommended sampling, a chat-template scan for
thinking control and tool support, the model's INPUT MODALITIES (``config.json``
``vision_config``/``audio_config``; for a repo without a readable ``config.json``,
its ``processor_config.json``/``preprocessor_config.json`` or Mistral
``params.json``; the repo's ``pipeline_tag``) and its MODEL TYPE (the
``pipeline_tag``). Gated repos refuse file reads anonymously, so the public
metadata call's ``pipeline_tag``/``config.architectures``/``siblings`` are the
evidence that still resolves them. Never guesses a repo: a caller passes a
``model_key`` that some other rule already resolved to a Hugging Face repo id
(:mod:`clio_agent.providers.capabilities.link`'s ``hf_vllm_root`` /
``hf_ollama_pull`` / ``hf_llama_cpp_router`` rules, or a repo id an overlay
entry names directly); this module's own job stops at CONFIRMING that repo
exists and, for a GGUF repo, following ``cardData.base_model`` to the model
that actually ships ``generation_config.json`` / a chat template.

Every fetch goes through :class:`~clio_agent.providers.fetched_catalog.FetchedCatalog`
(the SAME fetch -> disk-cache (TTL) -> validate -> last-good pipeline the Claude
model catalog and the overlay reuse -- brief Part 8 point 1: "don't write a
second fetcher"), pinned to the repo's own commit SHA so a cached read is
always reproducible: the metadata call resolves ``sha``, and the file fetch
URL embeds it, so a new commit is a NEW cache key/URL rather than a
silently-changing one -- which is also brief 5.6's "Model records: invalidated
when ... the Hugging Face commit ... changes", implemented by nothing more
than the cache key naturally changing when the byte content it fetches is
new evidence.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from clio_agent.providers.capabilities.hf_modalities import (
    PIPELINE_MODEL_TYPES,
    PROCESSOR_FILES,
    modalities_from_repo,
)
from clio_agent.providers.capabilities.records import (
    Fact,
    ModelCapabilities,
    ThinkingSpec,
    model_type_fact,
    unknown,
)
from clio_agent.providers.fetched_catalog import FetchedCatalog, FetchedCatalogUnavailable

logger = logging.getLogger(__name__)

_HF_API_ROOT = "https://huggingface.co"
#: How long a repo's metadata (its current commit SHA) is trusted before
#: re-resolving. Short relative to the pinned file fetch itself (which is keyed
#: by SHA and therefore safe to cache far longer) -- this TTL only bounds how
#: quickly clio notices a NEW commit exists at all.
METADATA_TTL_S = 6 * 3600.0
#: Once a (repo, sha, filename) triple is fetched, its content can never change
#: (the SHA pins it) -- long TTL is purely about not re-hitting the network for
#: an immutable answer, not about freshness.
FILE_TTL_S = 30 * 24 * 3600.0
#: How long a failed read (a repo that does not exist, a gated file answering
#: 401, a file the commit does not have) is remembered before asking again.
#: :class:`FetchedCatalog` keeps last-good data but never caches a MISS, so
#: without this every handshake re-asks huggingface.co about every private or
#: gated id it serves.
MISS_TTL_S = METADATA_TTL_S

_misses: dict[str, float] = {}
_misses_lock = threading.Lock()


def _recent_miss(key: str) -> bool:
    with _misses_lock:
        at = _misses.get(key)
        if at is None:
            return False
        if time.monotonic() - at < MISS_TTL_S:
            return True
        del _misses[key]
        return False


def _record_miss(key: str, reason: str) -> None:
    with _misses_lock:
        _misses[key] = time.monotonic()
    logger.info("hf_repo: reason=%s key=%s (not re-asked for %.0fs)", reason, key, MISS_TTL_S)


def clear_miss_cache() -> None:
    """Forget every remembered miss (tests; a user signing in to a gated repo)."""
    with _misses_lock:
        _misses.clear()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize(text: str) -> str:
    """Make a Hugging Face repo id / filename safe as a cache-file stem component."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)


def _parse_json_object(payload: bytes) -> dict[str, Any]:
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def _parse_text(payload: bytes) -> str:
    return payload.decode("utf-8")


@lru_cache(maxsize=256)
def _metadata_catalog(repo: str) -> FetchedCatalog[dict[str, Any]]:
    # cached (per brief Part 8's own fetch-code reuse note, and
    # fetched_catalog.py's own "share the refresh lock" guidance) so two
    # concurrent callers resolving the SAME repo serialize on one lock/cache
    # file instead of racing two independent network fetches.
    return FetchedCatalog(
        name=f"hf_repo_meta_{_sanitize(repo)}",
        url=f"{_HF_API_ROOT}/api/models/{repo}",
        parse=_parse_json_object,
        ttl_s=METADATA_TTL_S,
    )


@lru_cache(maxsize=512)
def _file_catalog(repo: str, sha: str, filename: str) -> FetchedCatalog[str]:
    return FetchedCatalog(
        name=f"hf_repo_file_{_sanitize(repo)}_{_sanitize(sha)[:12]}_{_sanitize(filename)}",
        url=f"{_HF_API_ROOT}/{repo}/raw/{sha}/{filename}",
        parse=_parse_text,
        ttl_s=FILE_TTL_S,
    )


@dataclass(frozen=True)
class RepoResolution:
    """The repo this layer actually reads files from, plus its pinned commit.

    Attributes:
        repo: The ORIGINAL repo id files are fetched from -- ``requested`` itself
            unless ``cardData.base_model`` redirected to it (a GGUF repo case).
        sha: The commit SHA every file fetch is pinned to.
        requested: The repo id the caller actually asked about (may differ from
            ``repo`` for a GGUF repo).
        pipeline_tag: The repo's Hub ``pipeline_tag`` (``""`` when unset).
        architectures: ``config.architectures`` as the public metadata call
            reports it -- readable even for a gated repo.
        siblings: The file names at this commit; empty when the metadata did
            not list them (then a file read is simply attempted).
    """

    repo: str
    sha: str
    requested: str
    pipeline_tag: str = ""
    architectures: tuple[str, ...] = ()
    siblings: frozenset[str] = field(default_factory=frozenset)

    def lists(self, filename: str) -> bool:
        """Whether this commit may hold ``filename`` (True when siblings are unknown)."""
        return not self.siblings or filename in self.siblings


_REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def is_repo_id(model_key: str) -> bool:
    """Whether ``model_key`` has the exact ``org/name`` shape of a Hub repo id.

    A shape check only -- it never names a DIFFERENT repo than the one asked
    about; :func:`resolve_repo` then confirms the repo exists (or skips the layer).
    """
    return bool(_REPO_ID.match(model_key or ""))


def resolve_repo(model_key: str, *, allow_fetch: bool = True) -> RepoResolution | None:
    """Confirm ``model_key`` names a real Hugging Face repo and resolve where its files live.

    Fetches ``GET /api/models/<model_key>``; a 404/network failure with no
    cached copy means "not a Hugging Face repo (or unreachable right now)" and
    this returns ``None`` -- brief 6.1: "If the repo can't be identified or
    fetched, skip this layer." For a GGUF repo (one that ships ``.gguf``
    weights or carries the ``gguf`` tag) with ``cardData.base_model`` set,
    follows it to the ORIGINAL repo and resolves ITS metadata too, since a GGUF
    repo essentially never ships ``generation_config.json``/a chat template
    itself. Any other repo is read as itself: an Instruct repo's
    ``base_model`` names the pre-trained BASE model, a different model.
    """
    meta = _fetch_metadata(model_key, allow_fetch=allow_fetch)
    if meta is None:
        return None
    sha = str(meta.get("sha") or "")
    if not sha:
        return None
    base_model = _base_model_from_card_data(meta.get("cardData"))
    if base_model is None or base_model == model_key or not _is_gguf_repo(meta):
        return _resolution(model_key, sha, model_key, meta)
    base_meta = _fetch_metadata(base_model, allow_fetch=allow_fetch)
    if base_meta is None:
        # The GGUF repo itself is real but its declared base model isn't
        # reachable -- do NOT silently fall back to scanning the GGUF repo's
        # own (almost certainly absent) files; that would be indistinguishable
        # from "this model has no thinking template" when really nothing was
        # ever read. Skip the layer honestly instead.
        return None
    base_sha = str(base_meta.get("sha") or "")
    if not base_sha:
        return None
    return _resolution(base_model, base_sha, model_key, base_meta)


def _is_gguf_repo(meta: Mapping[str, Any]) -> bool:
    """Whether a repo's metadata marks it as a GGUF quantization repo."""
    tags = meta.get("tags")
    if isinstance(tags, list) and any(str(tag).lower() == "gguf" for tag in tags):
        return True
    siblings = meta.get("siblings")
    return isinstance(siblings, list) and any(
        isinstance(item, Mapping) and str(item.get("rfilename") or "").lower().endswith(".gguf")
        for item in siblings
    )


def _resolution(repo: str, sha: str, requested: str, meta: Mapping[str, Any]) -> RepoResolution:
    config = meta.get("config")
    architectures = config.get("architectures") if isinstance(config, Mapping) else None
    siblings = meta.get("siblings")
    return RepoResolution(
        repo=repo,
        sha=sha,
        requested=requested,
        pipeline_tag=str(meta.get("pipeline_tag") or ""),
        architectures=tuple(str(a) for a in architectures)
        if isinstance(architectures, list)
        else (),
        siblings=frozenset(
            str(item.get("rfilename"))
            for item in siblings
            if isinstance(item, Mapping) and item.get("rfilename")
        )
        if isinstance(siblings, list)
        else frozenset(),
    )


def _fetch_metadata(repo: str, *, allow_fetch: bool) -> dict[str, Any] | None:
    key = f"meta:{repo}"
    if _recent_miss(key):
        return None
    try:
        result = _metadata_catalog(repo).get(allow_fetch=allow_fetch)
    except FetchedCatalogUnavailable:
        if allow_fetch:
            _record_miss(key, "hf_repo_metadata_unavailable")
        return None
    return result.data


def _fetch_text(resolution: RepoResolution, filename: str, *, allow_fetch: bool) -> str | None:
    """One file's text at the pinned commit, or ``None``; a failed read is remembered.

    A gated file (401 anonymously), an absent one (404) or an unreachable Hub is
    recorded as a miss for :data:`MISS_TTL_S` (typed ``hf_repo_file_unavailable``),
    so the next handshake does not ask again.
    """
    key = f"file:{resolution.repo}@{resolution.sha}/{filename}"
    if _recent_miss(key):
        return None
    try:
        result = _file_catalog(resolution.repo, resolution.sha, filename).get(
            allow_fetch=allow_fetch
        )
    except FetchedCatalogUnavailable:
        if allow_fetch:
            _record_miss(key, "hf_repo_file_unavailable")
        return None
    return result.data


def _json_object(text: str | None, *, repo: str, filename: str) -> dict[str, Any] | None:
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        logger.warning("hf_repo: reason=hf_repo_file_invalid_json repo=%s file=%s", repo, filename)
        return None
    return data if isinstance(data, dict) else None


def fetch_json_file(
    resolution: RepoResolution, filename: str, *, allow_fetch: bool = True
) -> dict[str, Any] | None:
    """One JSON file at the pinned commit, or ``None`` (absent, gated, unreachable, invalid).

    A file the commit's ``siblings`` do not list is never requested.
    """
    if not resolution.lists(filename):
        return None
    text = _fetch_text(resolution, filename, allow_fetch=allow_fetch)
    return _json_object(text, repo=resolution.repo, filename=filename)


def _base_model_from_card_data(card_data: Any) -> str | None:
    if not isinstance(card_data, dict):
        return None
    base_model = card_data.get("base_model")
    if isinstance(base_model, str) and base_model.strip():
        return base_model.strip()
    if isinstance(base_model, list) and base_model:
        first = base_model[0]
        return first.strip() if isinstance(first, str) and first.strip() else None
    return None


def fetch_generation_config(
    resolution: RepoResolution, *, allow_fetch: bool = True
) -> dict[str, Any] | None:
    """``generation_config.json`` at the pinned commit, or ``None`` when absent/unreachable."""
    text = _fetch_text(resolution, "generation_config.json", allow_fetch=allow_fetch)
    return _json_object(text, repo=resolution.repo, filename="generation_config.json")


#: Tried in this order: the standalone template file first (the more explicit,
#: single-purpose location), then the field inside tokenizer_config.json.
_TEMPLATE_FILENAMES: tuple[str, ...] = ("chat_template.jinja", "tokenizer_config.json")


def fetch_chat_template(resolution: RepoResolution, *, allow_fetch: bool = True) -> str | None:
    """The repo's chat template text, from ``chat_template.jinja`` or ``tokenizer_config.json``."""
    template = _fetch_text(resolution, "chat_template.jinja", allow_fetch=allow_fetch)
    if template is not None:
        return template
    text = _fetch_text(resolution, "tokenizer_config.json", allow_fetch=allow_fetch)
    data = _json_object(text, repo=resolution.repo, filename="tokenizer_config.json")
    value = data.get("chat_template") if data is not None else None
    return value if isinstance(value, str) and value.strip() else None


def sampling_from_generation_config(config: Mapping[str, Any]) -> dict[str, float]:
    """Extract the recommended sampling fields ``generation_config.json`` sets.

    Only fields the request builder (Part 7) actually knows how to send are
    kept -- an unrecognized key in the file is not a sampling parameter CLIO
    can act on, so it is dropped rather than passed through blind.
    """
    out: dict[str, float] = {}
    for key in ("temperature", "top_p", "top_k", "repetition_penalty"):
        value = config.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = float(value)
    return out


# --------------------------------------------------------------------------- template scan (brief 6.1)

_ENABLE_THINKING_RE = re.compile(r"\benable_thinking\b")
_REASONING_EFFORT_RE = re.compile(r"\breasoning_effort\b")
_REASONING_EFFORT_VALUES_RE = re.compile(
    r"reasoning_effort.{0,80}?[\[\(]\s*((?:['\"][a-zA-Z0-9_]+['\"]\s*,?\s*)+)[\]\)]", re.DOTALL
)
_QUOTED_TOKEN_RE = re.compile(r"['\"]([a-zA-Z0-9_]+)['\"]")
_THINK_TAG_RE = re.compile(r"<think>")
_TOOLS_RE = re.compile(
    r"\btool_calls\b|\{\%-?\s*for\s+\w+\s+in\s+tools\s*-?\%\}|\bavailable_tools\b"
)


def _reasoning_effort_levels(template: str) -> tuple[str, ...]:
    match = _REASONING_EFFORT_VALUES_RE.search(template)
    if not match:
        return ()
    return tuple(_QUOTED_TOKEN_RE.findall(match.group(1)))


def scan_chat_template(template: str) -> ThinkingSpec:
    """Scan a chat template's TEXT for its thinking mechanism (brief 6.1).

    A heuristic over the RAW template source, not a Jinja execution -- exactly
    what the brief calls for ("a template scan is a heuristic"):

    * ``enable_thinking`` referenced -> ``mechanism="on_off"``.
    * ``reasoning_effort`` referenced WITH a literal set of accepted values ->
      ``mechanism="effort_levels"`` with those values as ``levels``.
    * a literal ``<think>`` block with NEITHER of the above controlling it ->
      ``mechanism="always_on"`` (the template always emits it).
    * none of the above -> ``mechanism="none"``.
    """
    if _ENABLE_THINKING_RE.search(template):
        return ThinkingSpec(mechanism="on_off", template_kwarg="enable_thinking")
    if _REASONING_EFFORT_RE.search(template):
        levels = _reasoning_effort_levels(template)
        return ThinkingSpec(
            mechanism="effort_levels",
            levels=levels,
            effort_by_level={level: level for level in levels},
            template_kwarg="reasoning_effort",
        )
    if _THINK_TAG_RE.search(template):
        return ThinkingSpec(mechanism="always_on")
    return ThinkingSpec(mechanism="none")


def scan_tool_support(template: str) -> bool:
    """Whether the template references tool-call blocks at all (brief 6.1)."""
    return bool(_TOOLS_RE.search(template))


# --------------------------------------------------------------------------- HfRepoSource


class HfRepoCatalogSource:
    """The real :class:`~clio_agent.providers.capabilities.model_sources.HfRepoSource`.

    ``facts(model_key)`` is synchronous (matching the ``HfRepoSource`` Protocol
    P4a defined) -- :class:`~clio_agent.providers.fetched_catalog.FetchedCatalog`
    itself is a synchronous, blocking-on-cache-miss fetch, the same shape every
    other catalog source in this codebase (models.dev, the Claude catalog,
    the overlay) already uses.
    """

    def __init__(self, *, allow_fetch: bool = True) -> None:
        self._allow_fetch = allow_fetch

    def facts(self, model_key: str) -> ModelCapabilities | None:
        resolution = resolve_repo(model_key, allow_fetch=self._allow_fetch)
        if resolution is None:
            return None
        observed_at = _now_iso()
        detail_repo = (
            resolution.repo
            if resolution.repo == resolution.requested
            else f"{resolution.repo} (via cardData.base_model of {resolution.requested})"
        )

        sampling: dict[str, float] = {}
        generation_config = fetch_generation_config(resolution, allow_fetch=self._allow_fetch)
        if generation_config is not None:
            sampling = sampling_from_generation_config(generation_config)

        thinking: ThinkingSpec | None = None
        tools_known = False
        tools_value = False
        template = fetch_chat_template(resolution, allow_fetch=self._allow_fetch)
        if template is not None:
            thinking = scan_chat_template(template)
            tools_known = True
            tools_value = scan_tool_support(template)

        config = fetch_json_file(resolution, "config.json", allow_fetch=self._allow_fetch)
        processor: dict[str, Any] | None = None
        params: dict[str, Any] | None = None
        if config is None:
            # Gated (401 anonymously) or absent: fall back to the processor
            # config / Mistral params the commit ships, when readable.
            for name in PROCESSOR_FILES:
                processor = fetch_json_file(resolution, name, allow_fetch=self._allow_fetch)
                if processor is not None:
                    break
            params = fetch_json_file(resolution, "params.json", allow_fetch=self._allow_fetch)
        modalities, modality_detail = modalities_from_repo(
            resolution, config=config, processor=processor, params=params
        )
        type_fact = model_type_fact(
            PIPELINE_MODEL_TYPES.get(resolution.pipeline_tag),
            source="hf_repo",
            observed_at=observed_at,
            detail=f"{detail_repo}: pipeline_tag={resolution.pipeline_tag!r}",
        )

        if not sampling and thinking is None and modalities is None and not type_fact.known:
            return None

        is_reasoning = thinking is not None and thinking.mechanism != "none"
        return ModelCapabilities(
            model_key=model_key,
            model_type=type_fact,
            input_modalities=(
                Fact(
                    modalities,
                    "hf_repo",
                    observed_at,
                    f"{detail_repo}@{resolution.sha[:12]}: {modality_detail}",
                )
                if modalities is not None
                else unknown(f"{detail_repo}: {modality_detail}")
            ),
            tools=(
                Fact(tools_value, "hf_repo", observed_at, f"{detail_repo}: template_scan")
                if tools_known
                else unknown()
            ),
            thinking=(
                Fact(thinking, "hf_repo", observed_at, f"{detail_repo}: template_scan")
                if thinking is not None
                else unknown()
            ),
            sampling_thinking=(
                Fact(sampling, "hf_repo", observed_at, f"{detail_repo}: generation_config.json")
                if sampling and is_reasoning
                else unknown()
            ),
            sampling_instruct=(
                Fact(sampling, "hf_repo", observed_at, f"{detail_repo}: generation_config.json")
                if sampling and not is_reasoning
                else unknown()
            ),
        )


__all__ = [
    "FILE_TTL_S",
    "METADATA_TTL_S",
    "MISS_TTL_S",
    "HfRepoCatalogSource",
    "RepoResolution",
    "clear_miss_cache",
    "fetch_chat_template",
    "fetch_generation_config",
    "fetch_json_file",
    "is_repo_id",
    "resolve_repo",
    "sampling_from_generation_config",
    "scan_chat_template",
    "scan_tool_support",
]
