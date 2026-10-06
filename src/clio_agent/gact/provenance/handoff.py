"""The provenance handoff: clio's EFFECTIVE provenance config, as one CLIO YAML file.

A provenance-aware Agent Blueprint MCP server (the ``spotter-ai`` watcher is the
first) reads "one explicit CLIO YAML path" to learn which agentic and artifact
query stores to connect to. clio's own config cannot serve as that file: most
of the values it needs are resolved IN CODE (the native JSONL journal root is
the app's ``semantic_traces`` directory unless ``provenance.agentic.jsonl.path``
overrides it; the artifact workspace root is the session's workspace), so a
reader of the raw ``config.yaml`` sees an unset journal path and refuses.

This module projects the values clio actually runs with into that file shape
(``provenance.agentic.*`` / ``provenance.artifacts.*``) and reports, as typed
problems, every requirement the projection cannot satisfy — so a caller can
refuse or grey out a dependent capability with the reason instead of starting a
server that dies on its config.

It is a derived projection, never a store: the file is rewritten from live
config on every call, lives under the regenerable cache dir, and nothing reads
it back into clio.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from clio_agent import conf, paths
from clio_agent.provenance_config import attention_capture_enabled, configured_provider_names

#: No agentic provenance provider is configured, so there is no store to query.
PROBLEM_NO_AGENTIC_PROVIDER = "provenance_agentic_disabled"
#: The query default names a provider the configured list does not enable.
PROBLEM_QUERY_PROVIDER_NOT_ENABLED = "provenance_query_provider_not_enabled"
#: The query default is Flowcept but no Flowcept settings file is configured.
PROBLEM_FLOWCEPT_SETTINGS_MISSING = "provenance_flowcept_settings_missing"
#: Native (JSONL) queries are selected but the running app exposes no journal root.
PROBLEM_JSONL_JOURNAL_UNAVAILABLE = "provenance_jsonl_journal_unavailable"
#: Artifact lineage is CMF but no CMF server URL is configured.
PROBLEM_CMF_SERVER_MISSING = "provenance_cmf_server_missing"
#: Native artifact lineage needs a workspace root and the caller has none.
PROBLEM_WORKSPACE_UNRESOLVED = "provenance_workspace_unresolved"

#: Closed set: problem code -> (what is wrong, what to enable). The second half
#: is the operator-facing remedy a UI shows next to a disabled capability.
HANDOFF_PROBLEMS: dict[str, tuple[str, str]] = {
    PROBLEM_NO_AGENTIC_PROVIDER: (
        "agentic provenance is disabled, so there is no execution record to query",
        "enable an agentic provenance provider (provenance.agentic.providers: [jsonl] "
        "or [jsonl, flowcept])",
    ),
    PROBLEM_QUERY_PROVIDER_NOT_ENABLED: (
        "the provenance query default names a provider that is not enabled",
        "add the provider named by provenance.agentic.query_default to "
        "provenance.agentic.providers, or set the query default to native",
    ),
    PROBLEM_FLOWCEPT_SETTINGS_MISSING: (
        "provenance queries default to Flowcept but no Flowcept settings file is configured",
        "set provenance.agentic.flowcept.settings_path to your Flowcept settings file",
    ),
    PROBLEM_JSONL_JOURNAL_UNAVAILABLE: (
        "native provenance queries are selected but no native journal is being written",
        "enable the native journal (provenance.agentic.providers includes jsonl)",
    ),
    PROBLEM_CMF_SERVER_MISSING: (
        "artifact lineage uses CMF but no CMF server is configured",
        "set provenance.artifacts.cmf.server_url, or use the native artifact provider",
    ),
    PROBLEM_WORKSPACE_UNRESOLVED: (
        "native artifact lineage needs a workspace and none is selected",
        "open the session in a workspace",
    ),
}

_NATIVE_ALIASES = {"native", "file", "jsonl"}


@dataclass(frozen=True)
class HandoffProblem:
    """One typed reason the handoff cannot describe a working query provider."""

    code: str

    @property
    def detail(self) -> str:
        """What is wrong."""

        return HANDOFF_PROBLEMS[self.code][0]

    @property
    def remedy(self) -> str:
        """What to enable to fix it."""

        return HANDOFF_PROBLEMS[self.code][1]


@dataclass(frozen=True)
class ProvenanceHandoff:
    """The projected document plus every unmet requirement."""

    document: dict[str, Any]
    problems: tuple[HandoffProblem, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        """True when a reader of the document can open a query provider."""

        return not self.problems


def _text(key: str, env: str, default: str = "") -> str:
    return conf.resolve(key, env=env, default=default, cast=conf.as_str).strip()


def _journal_root(app: Any) -> Path | None:
    """The native journal root the running app writes, from its public seam."""

    backend = getattr(app, "state", None)
    backend = getattr(backend, "semantic_trace_backend", None) if backend is not None else None
    replay_paths = getattr(backend, "replay_paths", ()) if backend is not None else ()
    for raw in replay_paths or ():
        return raw if isinstance(raw, Path) else Path(str(raw))
    return None


def build_provenance_handoff(app: Any, *, workspace_root: Path | None) -> ProvenanceHandoff:
    """Project clio's effective provenance config into the CLIO YAML shape.

    Args:
        app: The running GACT app (its semantic trace backend names the native
            journal root; its artifact backend names the artifact provider).
        workspace_root: The session's workspace root, used for native artifact
            lineage. ``None`` when the caller has no workspace.

    Returns:
        The document and the typed problems, in the order a reader would hit them.
    """

    problems: list[HandoffProblem] = []
    providers = configured_provider_names()
    query_default = _text(
        "provenance.agentic.query_default", "CLIO_PROVENANCE_QUERY_DEFAULT", "native"
    ).lower()
    query_is_native = query_default in _NATIVE_ALIASES
    agentic: dict[str, Any] = {"providers": list(providers), "query_default": query_default}

    if not providers:
        problems.append(HandoffProblem(PROBLEM_NO_AGENTIC_PROVIDER))
    elif (query_is_native and "jsonl" not in providers) or (
        not query_is_native and query_default not in providers
    ):
        problems.append(HandoffProblem(PROBLEM_QUERY_PROVIDER_NOT_ENABLED))

    if "jsonl" in providers:
        journal = _journal_root(app)
        if journal is not None:
            agentic["jsonl"] = {"path": str(journal)}
        elif query_is_native:
            problems.append(HandoffProblem(PROBLEM_JSONL_JOURNAL_UNAVAILABLE))

    if "flowcept" in providers or query_default == "flowcept":
        settings_path = _text("provenance.agentic.flowcept.settings_path", "FLOWCEPT_SETTINGS_PATH")
        if settings_path:
            agentic["flowcept"] = {"settings_path": str(Path(settings_path).expanduser())}
        elif query_default == "flowcept":
            problems.append(HandoffProblem(PROBLEM_FLOWCEPT_SETTINGS_MISSING))

    artifact_backend = getattr(getattr(app, "state", None), "artifact_provenance_backend", None)
    artifact_provider = str(getattr(artifact_backend, "provider_name", "native") or "native")
    artifacts: dict[str, Any] = {"provider": artifact_provider}
    if artifact_provider == "cmf":
        server_url = _text("provenance.artifacts.cmf.server_url", "CLIO_CMF_SERVER_URL")
        if not server_url:
            problems.append(HandoffProblem(PROBLEM_CMF_SERVER_MISSING))
        artifacts["cmf"] = {
            "server_url": server_url,
            "pipeline_name": _text(
                "provenance.artifacts.cmf.pipeline_name", "CLIO_CMF_PIPELINE_NAME", "clio-agent"
            ),
        }
    if artifact_provider == "native" or query_is_native:
        if workspace_root is not None:
            artifacts["native"] = {"workspace_root": str(workspace_root)}
        else:
            problems.append(HandoffProblem(PROBLEM_WORKSPACE_UNRESOLVED))

    document: dict[str, Any] = {"provenance": {"agentic": agentic, "artifacts": artifacts}}
    capture_root = _text("provenance.attention.files_dir", "CLIO_PROVENANCE_ATTENTION_FILES_DIR")
    if attention_capture_enabled() and capture_root:
        # Dotted keys preserve the existing boolean provenance.attention setting.
        document["provenance.attention.files_dir"] = str(Path(capture_root).expanduser().resolve())
    return ProvenanceHandoff(document=document, problems=tuple(problems))


def handoff_path(workspace_root: Path | None) -> Path:
    """Where the handoff for ``workspace_root`` is written (regenerable cache)."""

    key = str(workspace_root.resolve()) if workspace_root is not None else "<no-workspace>"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return paths.user_cache_dir() / "provenance-handoff" / f"{digest}.yaml"


def write_provenance_handoff(
    app: Any, *, workspace_root: Path | None
) -> tuple[Path, ProvenanceHandoff]:
    """Build the handoff and write it atomically; return its path and the build.

    The file is written even when the handoff has problems, so a reader that is
    started anyway fails on the same unmet requirement the problems name rather
    than on a missing file.
    """

    handoff = build_provenance_handoff(app, workspace_root=workspace_root)
    target = handoff_path(workspace_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(handoff.document, sort_keys=False)
    fd, tmp = tempfile.mkstemp(prefix=".handoff-", suffix=".yaml", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target, handoff


__all__ = [
    "HANDOFF_PROBLEMS",
    "HandoffProblem",
    "ProvenanceHandoff",
    "build_provenance_handoff",
    "handoff_path",
    "write_provenance_handoff",
]
