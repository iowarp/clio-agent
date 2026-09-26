"""Tests for the Hugging Face repo layer (model-capabilities brief Part 6.1).

Network is fully mocked -- ``httpx.get`` (as :mod:`clio_agent.providers.
fetched_catalog` calls it) is monkeypatched to a small in-memory URL router
serving the recorded fixtures under ``tests/fixtures/capabilities/hf_repo/``,
so no real request ever reaches huggingface.co (resource rule: no real network
probes). ``allow_pytest_tmp_path`` (autouse, ``tests/conftest.py``) already
points ``CLIO_USER_DIR`` at an isolated ``tmp_path``, so
:class:`~clio_agent.providers.fetched_catalog.FetchedCatalog`'s disk cache
never touches a real user directory either.

The chat-template SCAN itself (``scan_chat_template``/``scan_tool_support``) is
tested separately against hand-authored, clearly-labeled Qwen3-style,
gpt-oss-style and no-thinking templates (brief 9.2) -- representative of the
publicly documented ``enable_thinking``/``reasoning_effort`` conventions, not
verbatim downloads (no real network probe is available here).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from clio_agent.providers.capabilities import hf_repo

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "capabilities" / "hf_repo"


def _load_json(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _load_text(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_catalog_caches() -> None:
    """Drop the ``lru_cache``d :class:`FetchedCatalog` instances between tests.

    Each test gets a fresh ``CLIO_USER_DIR`` (``allow_pytest_tmp_path``), but
    the module-level ``lru_cache`` on ``_metadata_catalog``/``_file_catalog``
    persists the (stateless, config-only) ``FetchedCatalog`` OBJECTS across
    tests -- harmless for correctness (``cache_path`` re-resolves ``CLIO_USER_DIR``
    live on every access) but cleared anyway so each test's assertions about
    *how many network calls happened* stay independent.
    """
    hf_repo._metadata_catalog.cache_clear()
    hf_repo._file_catalog.cache_clear()
    hf_repo.clear_miss_cache()
    yield
    hf_repo._metadata_catalog.cache_clear()
    hf_repo._file_catalog.cache_clear()
    hf_repo.clear_miss_cache()


class _UrlRouter:
    """Routes ``httpx.get(url, ...)`` to a canned ``(status, json_or_text)`` by exact URL."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, Any]] = {}
        self.requested: list[str] = []

    def add_json(self, url: str, payload: Any, status: int = 200) -> None:
        self.routes[url] = (status, json.dumps(payload).encode("utf-8"))

    def add_text(self, url: str, text: str, status: int = 200) -> None:
        self.routes[url] = (status, text.encode("utf-8"))

    def __call__(self, url: str, **_: object) -> httpx.Response:
        self.requested.append(url)
        if url not in self.routes:
            return httpx.Response(
                404, content=b'{"error":"not found"}', request=httpx.Request("GET", url)
            )
        status, content = self.routes[url]
        return httpx.Response(status, content=content, request=httpx.Request("GET", url))


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> _UrlRouter:
    router = _UrlRouter()
    from clio_agent.providers import fetched_catalog as fetched_catalog_module

    monkeypatch.setattr(fetched_catalog_module.httpx, "get", router)
    return router


# --------------------------------------------------------------------------- resolve_repo


def test_resolve_repo_direct_hit(router: _UrlRouter) -> None:
    router.add_json(
        "https://huggingface.co/api/models/Qwen/Qwen3-8B", _load_json("qwen3_8b_meta.json")
    )

    resolution = hf_repo.resolve_repo("Qwen/Qwen3-8B")

    assert resolution is not None
    assert resolution.repo == "Qwen/Qwen3-8B"
    assert resolution.sha == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert resolution.requested == "Qwen/Qwen3-8B"


def test_resolve_repo_follows_gguf_base_model(router: _UrlRouter) -> None:
    router.add_json(
        "https://huggingface.co/api/models/bartowski/Qwen3-8B-GGUF",
        _load_json("qwen3_8b_gguf_meta.json"),
    )
    router.add_json(
        "https://huggingface.co/api/models/Qwen/Qwen3-8B", _load_json("qwen3_8b_meta.json")
    )

    resolution = hf_repo.resolve_repo("bartowski/Qwen3-8B-GGUF")

    assert resolution is not None
    assert resolution.repo == "Qwen/Qwen3-8B"
    assert resolution.requested == "bartowski/Qwen3-8B-GGUF"
    assert resolution.sha == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_resolve_repo_returns_none_when_repo_does_not_exist(router: _UrlRouter) -> None:
    # no route registered -> the fake 404s, matching a real "repo not found".
    resolution = hf_repo.resolve_repo("not-a-real/repo")

    assert resolution is None


def test_resolve_repo_skips_the_layer_when_base_model_is_unreachable(router: _UrlRouter) -> None:
    router.add_json(
        "https://huggingface.co/api/models/bartowski/Qwen3-8B-GGUF",
        _load_json("qwen3_8b_gguf_meta.json"),
    )
    # Qwen/Qwen3-8B itself is NOT registered -> 404.

    resolution = hf_repo.resolve_repo("bartowski/Qwen3-8B-GGUF")

    assert resolution is None


def test_resolve_repo_never_guesses_when_fetch_disabled_and_uncached() -> None:
    resolution = hf_repo.resolve_repo("Qwen/Qwen3-8B", allow_fetch=False)

    assert resolution is None


# --------------------------------------------------------------------------- fetch_generation_config / fetch_chat_template


def test_fetch_generation_config_reads_sampling(router: _UrlRouter) -> None:
    resolution = hf_repo.RepoResolution(repo="Qwen/Qwen3-8B", sha="aaa", requested="Qwen/Qwen3-8B")
    router.add_text(
        "https://huggingface.co/Qwen/Qwen3-8B/raw/aaa/generation_config.json",
        _load_text("qwen3_8b_generation_config.json"),
    )

    config = hf_repo.fetch_generation_config(resolution)

    assert config is not None
    assert config["temperature"] == 0.6


def test_fetch_generation_config_absent_is_none_not_an_error(router: _UrlRouter) -> None:
    resolution = hf_repo.RepoResolution(repo="Qwen/Qwen3-8B", sha="aaa", requested="Qwen/Qwen3-8B")

    assert hf_repo.fetch_generation_config(resolution) is None


def test_fetch_chat_template_prefers_the_standalone_jinja_file(router: _UrlRouter) -> None:
    resolution = hf_repo.RepoResolution(repo="Qwen/Qwen3-8B", sha="aaa", requested="Qwen/Qwen3-8B")
    router.add_text(
        "https://huggingface.co/Qwen/Qwen3-8B/raw/aaa/chat_template.jinja",
        _load_text("qwen3_chat_template.jinja"),
    )

    template = hf_repo.fetch_chat_template(resolution)

    assert template is not None
    assert "enable_thinking" in template


def test_fetch_chat_template_falls_back_to_tokenizer_config(router: _UrlRouter) -> None:
    resolution = hf_repo.RepoResolution(repo="Qwen/Qwen3-8B", sha="aaa", requested="Qwen/Qwen3-8B")
    # no chat_template.jinja route registered -> 404, falls back:
    router.add_json(
        "https://huggingface.co/Qwen/Qwen3-8B/raw/aaa/tokenizer_config.json",
        {"chat_template": "{{ messages }}", "eos_token": "<|end|>"},
    )

    template = hf_repo.fetch_chat_template(resolution)

    assert template == "{{ messages }}"


def test_fetch_chat_template_none_when_neither_file_exists(router: _UrlRouter) -> None:
    resolution = hf_repo.RepoResolution(repo="Qwen/Qwen3-8B", sha="aaa", requested="Qwen/Qwen3-8B")

    assert hf_repo.fetch_chat_template(resolution) is None


# --------------------------------------------------------------------------- sampling_from_generation_config


def test_sampling_from_generation_config_keeps_only_recognized_fields() -> None:
    config = json.loads(_load_text("qwen3_8b_generation_config.json"))

    sampling = hf_repo.sampling_from_generation_config(config)

    assert sampling == {"temperature": 0.6, "top_p": 0.95, "top_k": 20}
    assert "do_sample" not in sampling
    assert "bos_token_id" not in sampling


# --------------------------------------------------------------------------- scan_chat_template (brief 9.2)


def test_scan_chat_template_qwen3_is_on_off() -> None:
    spec = hf_repo.scan_chat_template(_load_text("qwen3_chat_template.jinja"))

    assert spec.mechanism == "on_off"
    assert spec.template_kwarg == "enable_thinking"


def test_scan_chat_template_gpt_oss_is_effort_levels() -> None:
    spec = hf_repo.scan_chat_template(_load_text("gpt_oss_chat_template.jinja"))

    assert spec.mechanism == "effort_levels"
    assert spec.levels == ("low", "medium", "high")
    assert spec.template_kwarg == "reasoning_effort"


def test_scan_chat_template_plain_is_none() -> None:
    spec = hf_repo.scan_chat_template(_load_text("plain_no_thinking_chat_template.jinja"))

    assert spec.mechanism == "none"


def test_scan_chat_template_always_on_when_think_tag_has_no_controlling_toggle() -> None:
    always_on_template = "{{ '<think>' + messages[0].content + '</think>' }}"

    spec = hf_repo.scan_chat_template(always_on_template)

    assert spec.mechanism == "always_on"


def test_scan_tool_support() -> None:
    assert hf_repo.scan_tool_support(_load_text("qwen3_chat_template.jinja")) is True
    assert hf_repo.scan_tool_support(_load_text("gpt_oss_chat_template.jinja")) is True
    assert hf_repo.scan_tool_support(_load_text("plain_no_thinking_chat_template.jinja")) is False


# --------------------------------------------------------------------------- HfRepoCatalogSource end-to-end


def test_hf_repo_catalog_source_builds_model_capabilities(router: _UrlRouter) -> None:
    router.add_json(
        "https://huggingface.co/api/models/Qwen/Qwen3-8B", _load_json("qwen3_8b_meta.json")
    )
    router.add_text(
        "https://huggingface.co/Qwen/Qwen3-8B/raw/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/generation_config.json",
        _load_text("qwen3_8b_generation_config.json"),
    )
    router.add_text(
        "https://huggingface.co/Qwen/Qwen3-8B/raw/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/chat_template.jinja",
        _load_text("qwen3_chat_template.jinja"),
    )

    source = hf_repo.HfRepoCatalogSource()
    model = source.facts("Qwen/Qwen3-8B")

    assert model is not None
    assert model.thinking.value.mechanism == "on_off"
    assert model.thinking.source == "hf_repo"
    assert model.tools.value is True
    # a reasoning-capable model's recommended sampling lands under sampling_thinking
    assert model.sampling_thinking.value == {"temperature": 0.6, "top_p": 0.95, "top_k": 20}
    assert not model.sampling_instruct.known


def test_hf_repo_catalog_source_returns_none_for_an_unresolvable_repo(router: _UrlRouter) -> None:
    source = hf_repo.HfRepoCatalogSource()

    assert source.facts("no-such/repo") is None


def test_hf_repo_catalog_source_is_wired_into_model_sources_precedence(router: _UrlRouter) -> None:
    """The exact P4a interface point: ``resolve_model_capabilities(hf_repo=...)``."""
    from clio_agent.providers.capabilities.model_sources import resolve_model_capabilities

    router.add_json(
        "https://huggingface.co/api/models/Qwen/Qwen3-8B", _load_json("qwen3_8b_meta.json")
    )
    router.add_text(
        "https://huggingface.co/Qwen/Qwen3-8B/raw/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/chat_template.jinja",
        _load_text("qwen3_chat_template.jinja"),
    )

    merged = resolve_model_capabilities(
        "Qwen/Qwen3-8B", hf_repo=hf_repo.HfRepoCatalogSource(), community_lookup_id=""
    )

    assert merged.thinking.value.mechanism == "on_off"
    assert merged.thinking.source == "hf_repo"


# --------------------------------------------------------------------------- modalities + model type (recorded Hub responses)


def _alcf_meta(stem: str) -> dict[str, Any]:
    return _load_json(f"alcf_{stem}_meta.json")


def _resolution_from(stem: str) -> hf_repo.RepoResolution:
    meta = _alcf_meta(stem)
    return hf_repo._resolution(meta["id"], meta["sha"], meta["id"], meta)


def test_an_instruct_repos_base_model_is_not_followed(router: _UrlRouter) -> None:
    """``cardData.base_model`` on an Instruct repo names the BASE model: read the repo itself.

    Following it (as for a GGUF repo) redirected Llama-4-Maverick-Instruct to the
    pre-trained base, which 404s anonymously, and the whole layer was skipped.
    """
    meta = _alcf_meta("meta-llama_Llama-4-Maverick-17B-128E-Instruct")
    assert meta["cardData"]["base_model"]
    router.add_json(f"https://huggingface.co/api/models/{meta['id']}", meta)

    resolution = hf_repo.resolve_repo(meta["id"])

    assert resolution is not None
    assert resolution.repo == meta["id"]
    assert resolution.pipeline_tag == "image-text-to-text"
    assert resolution.architectures == ("Llama4ForConditionalGeneration",)
    assert "processor_config.json" in resolution.siblings


def test_gated_vision_repo_resolves_from_its_public_metadata() -> None:
    for stem in (
        "meta-llama_Llama-3.2-90B-Vision-Instruct",
        "meta-llama_Llama-4-Maverick-17B-128E-Instruct",
        "meta-llama_Llama-4-Scout-17B-16E-Instruct",
        "google_gemma-3-27b-it",
    ):
        modalities, detail = hf_repo.modalities_from_repo(_resolution_from(stem), config=None)
        assert modalities == frozenset({"text", "image"}), stem
        assert "pipeline_tag=image-text-to-text" in detail


def test_config_json_vision_and_audio_configs_name_their_modalities() -> None:
    resolution = _resolution_from("google_gemma-4-E4B-it")
    config = _load_json("alcf_google_gemma-4-E4B-it__config.json")

    modalities, detail = hf_repo.modalities_from_repo(resolution, config=config)

    assert modalities == frozenset({"text", "image", "audio"})
    assert "config.json vision_config" in detail
    assert "config.json audio_config" in detail


def test_a_repo_without_config_json_reads_its_processor_and_params() -> None:
    """Mistral-Large-3 ships ``params.json`` + ``processor_config.json``, no ``config.json``."""
    resolution = _resolution_from("mistralai_Mistral-Large-3-675B-Instruct-2512")
    assert not resolution.lists("config.json")
    processor = _load_json(
        "alcf_mistralai_Mistral-Large-3-675B-Instruct-2512__processor_config.json"
    )
    params = _load_json("alcf_mistralai_Mistral-Large-3-675B-Instruct-2512__params.json")

    assert hf_repo.modalities_from_repo(resolution, config=None, processor=processor)[
        0
    ] == frozenset({"text", "image"})
    assert hf_repo.modalities_from_repo(resolution, config=None, params=params)[0] == frozenset(
        {"text", "image"}
    )


def test_a_causal_lm_without_a_processor_is_known_text_only() -> None:
    modalities, detail = hf_repo.modalities_from_repo(
        _resolution_from("meta-llama_Llama-3.3-70B-Instruct"), config=None
    )
    assert modalities == frozenset({"text"})
    assert "LlamaForCausalLM" in detail


def test_a_multimodal_architecture_with_no_named_modality_stays_unknown() -> None:
    resolution = hf_repo.RepoResolution(
        repo="acme/vlm",
        sha="c" * 40,
        requested="acme/vlm",
        architectures=("AcmeForConditionalGeneration",),
        siblings=frozenset({"config.json", "preprocessor_config.json"}),
    )
    modalities, detail = hf_repo.modalities_from_repo(resolution, config=None)
    assert modalities is None
    assert "no file names the modality" in detail


def test_pipeline_tag_decides_the_task() -> None:
    assert hf_repo.PIPELINE_TASKS["feature-extraction"] == "feature-extraction"
    assert hf_repo.PIPELINE_TASKS["image-text-to-text"] == "image-text-to-text"
    assert hf_repo.PIPELINE_TASKS["sentence-similarity"] == "feature-extraction"
    assert hf_repo.PIPELINE_TASKS["summarization"] == "summarization"  # every Hub id verbatim
    assert "not-a-hub-tag" not in hf_repo.PIPELINE_TASKS


def test_embedding_repo_facts_carry_the_feature_extraction_task(router: _UrlRouter) -> None:
    meta = _alcf_meta("Salesforce_SFR-Embedding-Mistral")
    router.add_json(f"https://huggingface.co/api/models/{meta['id']}", meta)
    router.add_text(
        f"https://huggingface.co/{meta['id']}/raw/{meta['sha']}/config.json",
        _load_text("alcf_Salesforce_SFR-Embedding-Mistral__config.json"),
    )

    facts = hf_repo.HfRepoCatalogSource().facts(meta["id"])

    assert facts is not None
    assert facts.task.value == "feature-extraction"
    assert facts.task.source == "hf_repo"
    # MistralModel (no LM head) is not a causal LM: no modality claim either way.
    assert not facts.input_modalities.known


def test_gated_files_are_remembered_as_misses(router: _UrlRouter) -> None:
    meta = _alcf_meta("meta-llama_Llama-4-Scout-17B-16E-Instruct")
    router.add_json(f"https://huggingface.co/api/models/{meta['id']}", meta)
    for name in (
        "config.json",
        "generation_config.json",
        "chat_template.jinja",
        "tokenizer_config.json",
    ):
        router.add_text(
            f"https://huggingface.co/{meta['id']}/raw/{meta['sha']}/{name}", "gated", status=401
        )

    first = hf_repo.HfRepoCatalogSource().facts(meta["id"])
    asked = len(router.requested)
    second = hf_repo.HfRepoCatalogSource().facts(meta["id"])

    assert first is not None and second is not None
    assert first.input_modalities.value == frozenset({"text", "image"})
    assert len(router.requested) == asked  # nothing re-asked inside MISS_TTL_S


def test_is_repo_id_is_an_exact_shape_check() -> None:
    assert hf_repo.is_repo_id("meta-llama/Llama-4-Scout-17B-16E-Instruct")
    assert not hf_repo.is_repo_id("sam3")
    assert not hf_repo.is_repo_id("qwen3:8b")
    assert not hf_repo.is_repo_id("hf.co/org/repo")
    assert not hf_repo.is_repo_id("a/b/c")
