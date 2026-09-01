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
        from clio_agent.gact.artifacts.provenance.cmf import (
            CMFArtifactProvenanceProvider,
            CMFProviderConfig,
        )

        raw_metadata = conf.resolve(
            "provenance.artifacts.cmf.metadata_path",
            env="CLIO_CMF_METADATA_PATH",
            default="",
            cast=conf.as_str,
        ).strip()
        raw_artifacts = conf.resolve(
            "provenance.artifacts.cmf.artifact_root",
            env="CLIO_CMF_ARTIFACT_ROOT",
            default="",
            cast=conf.as_str,
        ).strip()
        provider = CMFArtifactProvenanceProvider(
            CMFProviderConfig(
                python=conf.resolve(
                    "provenance.artifacts.cmf.python",
                    env="CLIO_CMF_PYTHON",
                    default="",
                    cast=conf.as_str,
                ).strip(),
                # Kept as a raw string (never a Path): when the interpreter is
                # a remote launcher the script path belongs to the worker's
                # host and must survive this host's path flavour untouched.
                worker_script=conf.resolve(
                    "provenance.artifacts.cmf.worker_script",
                    env="CLIO_CMF_WORKER_SCRIPT",
                    default="",
                    cast=conf.as_str,
                ).strip(),
                metadata_path=(
                    Path(raw_metadata).expanduser()
                    if raw_metadata
                    else default_root / "cmf" / "mlmd.sqlite"
                ),
                artifact_root=(
                    Path(raw_artifacts).expanduser()
                    if raw_artifacts
                    else default_root / "cmf" / "artifacts"
                ),
                artifact_store=conf.resolve(
                    "provenance.artifacts.cmf.artifact_store",
                    env="CLIO_CMF_ARTIFACT_STORE",
                    default="reference",
                    cast=conf.as_str,
                )
                .strip()
                .lower(),
                pipeline_name=conf.resolve(
                    "provenance.artifacts.cmf.pipeline_name",
                    env="CLIO_CMF_PIPELINE_NAME",
                    default="clio-agent",
                    cast=conf.as_str,
                ).strip()
                or "clio-agent",
                server_url=conf.resolve(
                    "provenance.artifacts.cmf.server_url",
                    env="CLIO_CMF_SERVER_URL",
                    default="",
                    cast=conf.as_str,
                ).strip(),
                publish_timeout_s=conf.resolve(
                    "provenance.artifacts.cmf.publish_timeout_s",
                    env="CLIO_CMF_PUBLISH_TIMEOUT_S",
                    default=30.0,
                    cast=conf.as_float,
                ),
            )
        )
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
