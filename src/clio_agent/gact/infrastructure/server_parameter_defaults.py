"""Model-derived defaults for managed vLLM's tool-call and reasoning parsers.

An agent product needs tool calling on by default (DIRECTIVES 14): when the user
leaves a parser unset, choose it from the model's own ``config.json`` family. A
user's explicit choice always wins; an unknown family keeps vLLM's default.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: model_type -> (tool-call parser, reasoning parser or "").
_FAMILY_PARSERS: dict[str, tuple[str, str]] = {
    "qwen3": ("hermes", "qwen3"),
    "qwen3_moe": ("hermes", "qwen3"),
    "qwen2": ("hermes", ""),
    "llama": ("llama3_json", ""),
    "mistral": ("mistral", ""),
    "gpt_oss": ("openai", "openai_gptoss"),
    "deepseek_v3": ("deepseek_v3", "deepseek_v3"),
}


def parser_defaults(model_dir: str, configuration: dict[str, str]) -> dict[str, str]:
    """Return ``param.*`` parser entries to add for a locally readable model directory."""
    try:
        config = json.loads((Path(model_dir) / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    family = str(config.get("model_type", "")).lower()
    parsers = _FAMILY_PARSERS.get(family)
    if parsers is None:
        logger.info("vllm parser defaults: reason=unknown_model_family family=%s", family)
        return {}
    tool, reasoning = parsers
    added: dict[str, str] = {}
    if "param.tool_call_parser" not in configuration:
        added["param.tool_call_parser"] = tool
    if reasoning and "param.reasoning_parser" not in configuration:
        added["param.reasoning_parser"] = reasoning
    if added:
        logger.info("vllm parser defaults: reason=model_family family=%s added=%s", family, added)
    return added
