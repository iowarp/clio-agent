"""Select the CMF write mode from the conf declaration alone.

The system semantics close over configuration: what an operator declares fully
determines which write path runs, and every unsupported combination refuses with
a typed reason from
:mod:`clio_agent.gact.artifacts.provenance.cmf_reasons` rather than degrading
quietly or requiring a manual step.

============================  =======================================
Declaration                   Mode
============================  =======================================
``server_url`` only           **server** -- the release path. Writes go
                              straight to the CMF server; no local CMF
                              runtime is needed on any client OS.
``python`` only               **worker** -- the isolated local MLMD
                              runtime writes; nothing is published.
both                          **worker+publish** -- the worker owns the
                              durable local store and publishes it,
                              which is what makes a push retryable
                              across a server outage.
neither                       refused, ``cmf_no_write_target``
============================  =======================================

``worker_url`` (deployment shape (d), an in-stack CMF write service) is refused
as ``cmf_worker_url_unsupported`` -- declared vocabulary, not yet a mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent import conf
from clio_agent.gact.artifacts.provenance.cmf_reasons import CMFRefusal

if TYPE_CHECKING:
    from clio_agent.gact.artifacts.provenance.protocol import ArtifactProvenanceProvider


def _text(key: str, env: str, default: str = "") -> str:
    return conf.resolve(key, env=env, default=default, cast=conf.as_str).strip()


def resolve_cmf_mode(*, server_url: str, python: str, worker_url: str = "") -> str:
    """Decide which CMF write mode a declaration selects.

    Args:
        server_url: ``provenance.artifacts.cmf.server_url``.
        python: ``provenance.artifacts.cmf.python``.
        worker_url: ``provenance.artifacts.cmf.worker_url`` (shape (d)).

    Returns:
        ``"server"``, ``"worker"`` or ``"worker+publish"``.

    Raises:
        CMFRefusal: Nothing declares a write target, or the declaration names an
            unimplemented one.
    """
    if worker_url.strip():
        raise CMFRefusal(
            "cmf_worker_url_unsupported",
            "provenance.artifacts.cmf.worker_url names an in-stack CMF write "
            "service that is not implemented; declare server_url instead",
            worker_url=worker_url.strip(),
        )
    has_server = bool(server_url.strip())
    has_python = bool(python.strip())
    if has_server and has_python:
        return "worker+publish"
    if has_server:
        return "server"
    if has_python:
        return "worker"
    raise CMFRefusal(
        "cmf_no_write_target",
        "the CMF provider is selected but neither "
        "provenance.artifacts.cmf.server_url nor .python is declared; "
        "set server_url alone for the supported deployment",
    )


def build_cmf_provider(default_root: Path) -> "ArtifactProvenanceProvider":
    """Build the CMF provider the declaration selects.

    Args:
        default_root: The artifact-store root used when no path is declared.

    Returns:
        The server-mode or local-worker provider.

    Raises:
        CMFRefusal: The declaration selects no supported write target.
    """
    from clio_agent.gact.artifacts.provenance.cmf import (  # noqa: PLC0415
        CMFArtifactProvenanceProvider,
        CMFArtifactStore,
    )

    server_url = _text("provenance.artifacts.cmf.server_url", "CLIO_CMF_SERVER_URL")
    python = _text("provenance.artifacts.cmf.python", "CLIO_CMF_PYTHON")
    worker_url = _text("provenance.artifacts.cmf.worker_url", "CLIO_CMF_WORKER_URL")
    mode = resolve_cmf_mode(server_url=server_url, python=python, worker_url=worker_url)
    config = _provider_config(default_root, server_url=server_url, python=python)
    if mode != "server":
        return CMFArtifactProvenanceProvider(config)

    from clio_agent.gact.artifacts.provenance.cmf_lineage_rest import (  # noqa: PLC0415
        CMFRestLineageReader,
    )
    from clio_agent.gact.artifacts.provenance.cmf_server_mode import (  # noqa: PLC0415
        CMFServerConfig,
        CMFServerModeProvider,
    )

    server_config = CMFServerConfig(
        server_url=server_url,
        pipeline_name=config.pipeline_name,
        publish_timeout_s=config.publish_timeout_s,
    )
    return CMFServerModeProvider(
        server_config,
        # Server mode keeps CLIO's custody options: the store hashes by
        # reference or writes a DVC-compatible CAS, neither of which needs
        # cmflib.
        store=CMFArtifactStore(config),
        reader=CMFRestLineageReader(
            server_url,
            config.pipeline_name,
            timeout_s=config.publish_timeout_s,
        ),
    )


def _provider_config(default_root: Path, *, server_url: str, python: str) -> Any:
    """Read the shared CMF configuration block."""
    from clio_agent.gact.artifacts.provenance.cmf import CMFProviderConfig  # noqa: PLC0415

    raw_metadata = _text("provenance.artifacts.cmf.metadata_path", "CLIO_CMF_METADATA_PATH")
    raw_artifacts = _text("provenance.artifacts.cmf.artifact_root", "CLIO_CMF_ARTIFACT_ROOT")
    return CMFProviderConfig(
        python=python,
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
        artifact_store=_text(
            "provenance.artifacts.cmf.artifact_store", "CLIO_CMF_ARTIFACT_STORE", "reference"
        ).lower(),
        pipeline_name=_text(
            "provenance.artifacts.cmf.pipeline_name", "CLIO_CMF_PIPELINE_NAME", "clio-agent"
        )
        or "clio-agent",
        server_url=server_url,
        publish_timeout_s=conf.resolve(
            "provenance.artifacts.cmf.publish_timeout_s",
            env="CLIO_CMF_PUBLISH_TIMEOUT_S",
            default=30.0,
            cast=conf.as_float,
        ),
    )


__all__ = ["build_cmf_provider", "resolve_cmf_mode"]
