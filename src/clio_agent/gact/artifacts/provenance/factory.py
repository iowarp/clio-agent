"""Configuration and lazy construction for the selected artifact provider."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from clio_agent import conf
from clio_agent.gact.artifacts.provenance.protocol import ArtifactProvenanceProvider
from clio_agent.gact.artifacts.provenance.selector import (
    DEFAULT_ARTIFACT_EVENTS,
    ArtifactProvenanceDispatcher,
)

if TYPE_CHECKING:
    from fastapi import FastAPI


def configured_artifact_provider_name() -> str:
    """Return the selected artifact provider (native by default)."""
    name = (
        conf.resolve(
            "provenance.artifacts.provider",
            env="CLIO_ARTIFACT_PROVENANCE_PROVIDER",
            default="native",
            cast=conf.as_str,
        )
        .strip()
        .lower()
    )
    if name not in {"native", "cmf"}:
        raise ValueError(f"unsupported artifact provenance provider: {name}")
    return name


def build_artifact_provenance_backend(
    app: "FastAPI", default_root: Path
) -> ArtifactProvenanceDispatcher:
    """Build the selected provider and its provider-scoped primary store."""
    name = configured_artifact_provider_name()
    if name == "native":
        storage = (
            conf.resolve(
                "provenance.artifacts.native.storage",
                env="CLIO_NATIVE_ARTIFACT_STORE",
                default="file",
                cast=conf.as_str,
            )
            .strip()
            .lower()
        )
        if storage != "file":
            raise ValueError(
                "native artifact storage currently supports 'file'; "
                f"configured value was {storage!r}"
            )
        from clio_agent.gact.artifacts.provenance.native import (
            NativeArtifactProvenanceProvider,
        )

        provider: ArtifactProvenanceProvider = NativeArtifactProvenanceProvider(app)
    else:
        # The CMF write mode is decided by the declaration alone -- server mode
        # (server_url), local worker (python), both, or a typed refusal. Owned
        # by cmf_mode so this factory stays a thin selector.
        from clio_agent.gact.artifacts.provenance.cmf_mode import build_cmf_provider

        provider = build_cmf_provider(default_root)
    include_events = frozenset(
        conf.resolve(
            "provenance.artifacts.include_events",
            env="CLIO_ARTIFACT_PROVENANCE_EVENTS",
            default=sorted(DEFAULT_ARTIFACT_EVENTS),
            cast=conf.as_csv,
        )
    )
    queue_size = conf.resolve(
        "provenance.artifacts.queue_size",
        env="CLIO_ARTIFACT_PROVENANCE_QUEUE_SIZE",
        default=4096,
        cast=conf.as_int,
    )
    if queue_size < 1:
        raise ValueError("provenance.artifacts.queue_size must be at least 1")
    return ArtifactProvenanceDispatcher(
        provider,
        include_events=include_events,
        queue_size=queue_size,
    )


__all__ = ["build_artifact_provenance_backend", "configured_artifact_provider_name"]
