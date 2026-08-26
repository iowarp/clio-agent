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


def native_durable_provenance_enabled() -> bool:
    """Whether ARC may release its event log after native persistence."""

    names = configured_provider_names()
    return "jsonl" in names or "factory" in names


def durable_trace_backend_name() -> str:
    """ARC's durable-trace decision, mirroring the provider ladder above.

    ``"file"`` when a configured provider keeps a replayable NATIVE copy
    (Flowcept alone is not permission to erase ARC history), ``"none"`` when
    providers are explicitly configured without one, the verbatim legacy
    backend name when only legacy settings exist, else the default
    (``"file"``, matching the ``["jsonl"]`` provider default).
    """

    providers_file = conf.store().file_value("provenance.agentic.providers")
    providers_env = os.environ.get("CLIO_PROVENANCE_PROVIDERS", "").strip()
    if providers_file is not conf.UNSET or providers_env:
        raw = providers_file if providers_file is not conf.UNSET else providers_env
        names = {name.strip().lower() for name in conf.as_csv(raw)}
        return "file" if names.intersection(_NATIVE_DURABLE) else "none"
    legacy_file = conf.store().file_value("trace.backend")
    legacy_env = os.environ.get("CLIO_SEMANTIC_TRACE_BACKEND", "").strip()
    if legacy_file is not conf.UNSET or legacy_env:
        return str(legacy_file if legacy_file is not conf.UNSET else legacy_env).strip().lower()
    return "file"
