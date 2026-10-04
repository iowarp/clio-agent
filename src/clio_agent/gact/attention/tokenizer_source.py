"""Resolve the model's tokenizer + chat template (files only, never weights).

Resolution order, first hit wins, every miss typed:

1. ``provenance.attention.tokenizer`` -- a local directory or a Hugging Face
   repo id, for air-gapped nodes or servers started with a custom template;
2. the served model's ``root`` from the vLLM endpoint's ``GET /v1/models``
   (vLLM reports the HF id it loaded there), fetched from the Hub.

Only ``tokenizer.json``, ``tokenizer_config.json`` and ``chat_template.jinja``
are downloaded. Renderers are cached per identity for the process lifetime.
"""

from __future__ import annotations

import threading
from pathlib import Path

from clio_agent import conf
from clio_agent.gact.attention.chat_render import ChatRenderer
from clio_agent.gact.attention.reasons import AttentionUnavailable

_TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json", "chat_template.jinja")
_lock = threading.Lock()
_renderers: dict[str, ChatRenderer] = {}
_served_roots: dict[tuple[str, str], str] = {}


def configured_tokenizer() -> str:
    """The operator override (directory or HF id), or ``""``."""
    return conf.resolve(
        "provenance.attention.tokenizer",
        env="CLIO_PROVENANCE_ATTENTION_TOKENIZER",
        default="",
        cast=conf.as_str,
    ).strip()


def served_model_root(api_base: str, served_model: str) -> str:
    """The HF id vLLM loaded for ``served_model`` (``/v1/models`` ``root``)."""
    key = (api_base.rstrip("/"), served_model)
    with _lock:
        if key in _served_roots:
            return _served_roots[key]
    import requests  # noqa: PLC0415

    try:
        response = requests.get(f"{key[0]}/models", timeout=10)
        response.raise_for_status()
        rows = response.json().get("data") or []
    except (requests.RequestException, ValueError) as exc:
        raise AttentionUnavailable(
            "attention_tokenizer_unavailable",
            f"GET {key[0]}/models failed: {type(exc).__name__}: {exc}",
        ) from exc
    root = next(
        (str(row.get("root") or "") for row in rows if row.get("id") == served_model),
        "",
    )
    if not root:
        raise AttentionUnavailable(
            "attention_tokenizer_unavailable",
            f"{key[0]}/models lists no root for served model {served_model!r}",
        )
    with _lock:
        _served_roots[key] = root
    return root


def renderer_for(identity: str) -> ChatRenderer:
    """A cached :class:`ChatRenderer` for a local directory or HF repo id."""
    with _lock:
        cached = _renderers.get(identity)
    if cached is not None:
        return cached
    path = Path(identity)
    try:
        if path.is_dir():
            renderer = ChatRenderer.from_dir(path, identity=identity)
        else:
            renderer = ChatRenderer.from_dir(_download(identity), identity=identity)
    except AttentionUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - every load failure becomes one typed reason
        raise AttentionUnavailable(
            "attention_tokenizer_unavailable",
            f"could not load tokenizer {identity!r}: {type(exc).__name__}: {exc}",
        ) from exc
    with _lock:
        _renderers[identity] = renderer
    return renderer


def resolve_renderer(api_base: str, served_model: str) -> ChatRenderer:
    """Write-path resolution: operator override, else the served model's root."""
    identity = configured_tokenizer() or served_model_root(api_base, served_model)
    return renderer_for(identity)


def _download(repo_id: str) -> Path:
    from huggingface_hub import snapshot_download  # noqa: PLC0415

    return Path(snapshot_download(repo_id, allow_patterns=list(_TOKENIZER_FILES)))


def clear_cache() -> None:
    """Drop cached renderers and served-model roots (tests, model swaps)."""
    with _lock:
        _renderers.clear()
        _served_roots.clear()
