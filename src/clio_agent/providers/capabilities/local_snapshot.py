"""Model facts read from a local model snapshot directory the endpoint serves.

A CLIO-managed vLLM deployment (and any server launched on a downloaded
snapshot) is addressed by the snapshot's absolute PATH, not a Hub ``org/name``
id, so the Hugging Face repo layer (:mod:`~clio_agent.providers.capabilities.
hf_repo`) never ran for it: no chat-template scan, so the thinking mechanism and
template tool support stayed unknown and a thinking level had nothing to map to
(F016/F017). The snapshot holds the very files the server loaded, so this layer
reads them from disk -- the same template/generation-config scan as the Hub
layer, no network.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clio_agent.providers.capabilities.hf_repo import (
    sampling_from_generation_config,
    scan_chat_template,
    scan_tool_support,
)
from clio_agent.providers.capabilities.records import Fact, ModelCapabilities, unknown

_LOGGER = logging.getLogger(__name__)

__all__ = ["LocalSnapshotSource", "is_local_snapshot"]


def is_local_snapshot(model_key: str) -> bool:
    """Whether ``model_key`` is an absolute path to a model directory (has ``config.json``)."""

    if not model_key:
        return False
    path = Path(model_key)
    try:
        return path.is_absolute() and (path / "config.json").is_file()
    except OSError:
        return False


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _LOGGER.warning("local_snapshot_file_unreadable path=%s error=%s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _chat_template(root: Path) -> str | None:
    """``chat_template.jinja`` first, then ``tokenizer_config.json``'s field (Hub order)."""

    try:
        text = (root / "chat_template.jinja").read_text(encoding="utf-8")
    except OSError:
        text = ""
    if text.strip():
        return text
    data = _read_json(root / "tokenizer_config.json")
    value = data.get("chat_template") if data is not None else None
    return value if isinstance(value, str) and value.strip() else None


class LocalSnapshotSource:
    """An ``HfRepoSource`` over a local snapshot directory (see the module docstring)."""

    def __init__(self, root: str) -> None:
        self._root = Path(root)

    def facts(self, model_key: str) -> ModelCapabilities | None:
        """Thinking/tools from the chat template and sampling from ``generation_config.json``."""

        root = self._root
        observed_at = datetime.now(timezone.utc).isoformat()
        detail = f"local snapshot {root}"
        template = _chat_template(root)
        generation = _read_json(root / "generation_config.json")
        sampling = sampling_from_generation_config(generation) if generation is not None else {}
        if template is None and not sampling:
            return None
        thinking = scan_chat_template(template) if template is not None else None
        is_reasoning = thinking is not None and thinking.mechanism != "none"
        return ModelCapabilities(
            model_key=model_key,
            tools=(
                Fact(
                    scan_tool_support(template), "hf_repo", observed_at, f"{detail}: template_scan"
                )
                if template is not None
                else unknown()
            ),
            thinking=(
                Fact(thinking, "hf_repo", observed_at, f"{detail}: template_scan")
                if thinking is not None
                else unknown()
            ),
            sampling_thinking=(
                Fact(sampling, "hf_repo", observed_at, f"{detail}: generation_config.json")
                if sampling and is_reasoning
                else unknown()
            ),
            sampling_instruct=(
                Fact(sampling, "hf_repo", observed_at, f"{detail}: generation_config.json")
                if sampling and not is_reasoning
                else unknown()
            ),
        )
