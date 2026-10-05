"""Agentic-provenance provider configuration — the ONE precedence ladder (#1247).

Both sides of the provenance split read the same decision: which agentic
providers are configured (new ``provenance.agentic.providers`` /
``CLIO_PROVENANCE_PROVIDERS``, with explicit-legacy ``trace.backend`` /
``CLIO_SEMANTIC_TRACE_BACKEND`` translation, then the default). Before this
module the ladder existed three times — ``gact/provenance/factory.py``,
``arc/memory.py``'s durable-trace decision, and the default literal — and the
copies could drift. ``arc/`` must stay free of gact imports, so the neutral
home is this top-level module; ``gact/provenance/factory.py`` re-exports its
public names unchanged.

The DEFAULT (``["jsonl"]`` — durable native provenance on) is decided HERE
and mirrored by ``config.defaults.yaml`` (drift-tested by
``tests/test_docs/test_env_reference.py``); change them together.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from clio_agent import conf

_DISABLED = {"", "none", "off", "disabled"}

#: Provider names that keep a REPLAYABLE native copy of the event stream.
_NATIVE_DURABLE = {"jsonl", "file", "native", "factory"}


def configured_provider_names() -> list[str]:
    """Resolve new configuration, translating explicit legacy settings only."""

    file_value = conf.store().file_value("provenance.agentic.providers")
    env_value = os.environ.get("CLIO_PROVENANCE_PROVIDERS", "").strip()
    if file_value is not conf.UNSET or env_value:
        raw = file_value if file_value is not conf.UNSET else env_value
        return _normalize_provider_names(conf.as_csv(raw))

    legacy_file = conf.store().file_value("trace.backend")
    legacy_env = os.environ.get("CLIO_SEMANTIC_TRACE_BACKEND", "").strip()
    if legacy_file is not conf.UNSET or legacy_env:
        legacy = str(legacy_file if legacy_file is not conf.UNSET else legacy_env).strip().lower()
        if legacy in _DISABLED:
            return []
        if legacy == "file":
            return ["jsonl"]
        if legacy in {"factory", "python_factory", "custom"}:
            return ["factory"]
        raise ValueError(f"unsupported semantic trace backend: {legacy}")

    return _normalize_provider_names(
        conf.resolve(
            "provenance.agentic.providers",
            env="CLIO_PROVENANCE_PROVIDERS",
            default=["jsonl"],
            cast=conf.as_csv,
        )
    )


def _normalize_provider_names(names: list[str]) -> list[str]:
    aliases = {"file": "jsonl", "native": "jsonl"}
    result: list[str] = []
    for raw_name in names:
        name = aliases.get(raw_name.strip().lower(), raw_name.strip().lower())
        if name in _DISABLED or name in result:
            continue
        if name not in {"jsonl", "flowcept", "factory"}:
            raise ValueError(f"unsupported agentic provenance provider: {name}")
        result.append(name)
    return result


def kvnorm_join_enabled() -> bool:
    """Whether the vLLM response id is stamped onto ``lm.call`` provenance records.

    The id (``chatcmpl-*``) is the join key between clio's
    ``ai_model_invocation`` stream and vllm-kvnorm's ``kv_token_importance``
    stream in one Flowcept store. Requires BOTH the explicit
    ``provenance.kvnorm`` opt-in AND Flowcept among the configured providers --
    without Flowcept there is no fused store to join, so the stamp stays off.
    """

    if not conf.resolve(
        "provenance.kvnorm",
        env="CLIO_PROVENANCE_KVNORM",
        default=False,
        cast=conf.as_bool,
    ):
        return False
    return "flowcept" in configured_provider_names()


def attention_capture_enabled() -> bool:
    """Whether model calls declare attention ranges for the Flowcept connector."""
    value = conf.resolve(
        "provenance.attention",
        env="CLIO_PROVENANCE_ATTENTION",
        default=False,
    )
    # Preserve the established boolean while allowing enabled + files_dir in one
    # YAML namespace. A files-only mapping does not opt the user into capture.
    if isinstance(value, Mapping):
        value = value.get("enabled", False)
    return conf.as_bool(value) and "flowcept" in configured_provider_names()


def response_id_join_enabled() -> bool:
    """Whether model provenance retains the provider response join key."""
    return kvnorm_join_enabled() or attention_capture_enabled()
