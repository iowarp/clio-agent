"""LOCAL-worker CMF artifact-provenance and DVC-local custody adapter.

CMF 0.1.0 declares Python below 3.12 and pins ``ml-metadata==1.15.0``.  CLIO
targets Python 3.12, so the real CMF/MLMD writer runs in a configured, isolated
CMF-compatible interpreter.  The parent process never imports ``cmflib`` and
the worker never changes CLIO's cwd or Git branch.

That interpreter is **local**, and only local.  ``ssh host /opt/cmf/bin/python``
is not product surface: CLIO cannot assume shell or SSH reach out of its own
container, so a deployment whose CMF/MLMD runtime cannot exist on this host
(no ml-metadata wheels for win32, say) uses **server mode** instead --
``provenance.artifacts.cmf.server_url`` alone, see
:mod:`clio_agent.gact.artifacts.provenance.cmf_server_mode`, which needs no
local CMF at all and is the release path.  Every unsupported combination here
refuses with a typed reason from
:mod:`clio_agent.gact.artifacts.provenance.cmf_reasons`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from clio_agent.gact.artifacts.cas import IngestedIdentity, ingest_identity
from clio_agent.gact.artifacts.provenance.cmf_reasons import CMFRefusal
from clio_agent.gact.artifacts.provenance.protocol import StorageReceipt
from clio_agent.gact.artifacts.records import Custody, EvidenceClass, IdentityEvidence
from clio_agent.gact.provenance.protocol import ProviderReceipt

if TYPE_CHECKING:
    from clio_schemas import ArtifactVersion

    from clio_agent.gact.artifacts.provenance.protocol import ArtifactStore
    from clio_agent.gact.semantic_events import SemanticEvent

_CHUNK_BYTES = 1024 * 1024
_DVC_OBJECT_PREFIX = "files/md5/"

#: Executables that start a runtime on ANOTHER host. Naming one as the "local"
#: interpreter is a configuration error with a typed reason, not a feature:
#: CLIO cannot assume shell or SSH reach out of its own container.
_REMOTE_LAUNCHERS = frozenset({"ssh", "docker", "podman", "srun", "sbatch", "kubectl", "wsl"})


@dataclass(frozen=True)
class CMFProviderConfig:
    """All CMF-specific runtime, metadata, and custody configuration."""

    #: A LOCAL interpreter path. Never a launcher command: CLIO cannot assume
    #: shell reach into another host, so ``ssh host python`` is not product
    #: surface (it is refused as ``cmf_local_runtime_unavailable``). A CMF
    #: runtime that cannot exist on this host is reached through server mode.
    python: str
    metadata_path: Path
    artifact_root: Path
    artifact_store: str = "reference"
    pipeline_name: str = "clio-agent"
    server_url: str = ""
    publish_timeout_s: float = 30.0

    def __post_init__(self) -> None:
        if self.artifact_store not in {"reference", "local"}:
            raise ValueError(
                "provenance.artifacts.cmf.artifact_store must be 'reference' or 'local'"
            )
        if not self.pipeline_name.strip():
            raise ValueError("provenance.artifacts.cmf.pipeline_name must not be empty")
        if self.publish_timeout_s <= 0:
            raise ValueError("provenance.artifacts.cmf.publish_timeout_s must be greater than zero")


class CMFBridgeError(RuntimeError):
    """Typed failure from the isolated CMF/MLMD runtime."""


class CMFBridge:
    """Line-delimited request client for the isolated real-CMF worker."""

    def __init__(self, config: CMFProviderConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._stderr: deque[str] = deque(maxlen=40)
        self._stderr_thread: threading.Thread | None = None

    def request(self, operation: str, **payload: Any) -> dict[str, Any]:
        """Send one serialized request and return its typed response."""
        with self._lock:
            process = self._ensure_process()
            assert process.stdin is not None
            assert process.stdout is not None
            request = {"operation": operation, **payload}
            try:
                process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
                process.stdin.flush()
                line = process.stdout.readline()
            except OSError as exc:
                raise CMFBridgeError(f"CMF worker transport failed: {exc}") from exc
            if not line:
                stderr = " | ".join(self._stderr)
                raise CMFBridgeError(
                    f"CMF worker exited without a response (rc={process.poll()}): {stderr}"
                )
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CMFBridgeError(f"CMF worker returned invalid JSON: {line[:300]!r}") from exc
            if not isinstance(response, dict) or not response.get("ok"):
                reason = str(response.get("error") if isinstance(response, dict) else response)
                raise CMFBridgeError(reason or "CMF worker request failed")
            return response

    def close(self) -> None:
        """Ask the worker to close its MLMD store and terminate it."""
        with self._lock:
            process = self._process
            self._process = None
            if process is None:
                return
            try:
                if process.poll() is None and process.stdin is not None:
                    process.stdin.write('{"operation":"close"}\n')
                    process.stdin.flush()
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()

    def _ensure_process(self) -> subprocess.Popen[str]:
        process = self._process
        if process is not None and process.poll() is None:
            return process
        launcher = resolve_local_worker_command(self.config.python)
        argv = _worker_argv(self.config, launcher)
        # The worker always runs on THIS machine, so the stores it opens are
        # ours to create.
        self.config.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.artifact_root.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(  # noqa: S603 - fixed worker with configured interpreter
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            # No creationflags branch: local mode refuses win32 outright
            # (cmf_local_runtime_unsupported_platform), so this Popen is only
            # ever reached on a POSIX host.
        )
        self._process = process
        assert process.stderr is not None
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(process.stderr,),
            name="CMFBridgeStderr",
            daemon=True,
        )
        self._stderr_thread.start()
        return process

    def _drain_stderr(self, stream: TextIO) -> None:
        for line in stream:
            clean = line.strip()
            if clean:
                self._stderr.append(clean)


def _resolve_python(configured: str) -> str:
    value = configured.strip()
    if not value:
        return ""
    path = Path(value).expanduser()
    if path.is_file():
        # A POSIX virtualenv's ``bin/python`` is normally a symlink to the base
        # interpreter.  Resolving that symlink discards the virtualenv launch
        # context and therefore its site-packages (including cmflib).  Keep the
        # configured executable path while still making a relative path absolute.
        return str(path.absolute())
    return shutil.which(value) or ""


def resolve_local_worker_command(configured: str, *, platform: str = sys.platform) -> list[str]:
    """Resolve ``provenance.artifacts.cmf.python`` into a LOCAL argv prefix.

    The setting names one local interpreter, and nothing else. A launcher
    *command* that reaches another host (``ssh host python``, ``docker exec``,
    ``srun``) is deliberately NOT product surface: CLIO cannot assume shell or
    SSH reach out of its own container, so a deployment whose CMF/MLMD runtime
    cannot exist on this host uses server mode instead, which needs no local
    CMF at all.

    Args:
        configured: The configured interpreter path.
        platform: The platform to judge against; a parameter (not a read of
            ``sys.platform``) so both branches are asserted on any host.

    Returns:
        The single-element argv prefix that starts the worker.

    Raises:
        CMFRefusal: ``cmf_no_write_target`` when nothing is configured;
            ``cmf_local_runtime_unavailable`` for a launcher command or an
            unresolvable path; ``cmf_local_runtime_unsupported_platform`` where
            no ml-metadata wheels exist.
    """
    value = configured.strip()
    if not value:
        raise CMFRefusal(
            "cmf_no_write_target",
            "provenance.artifacts.cmf.python is unset and no server_url was declared",
        )
    if any(character.isspace() for character in value) and not Path(value).expanduser().is_file():
        raise CMFRefusal(
            "cmf_local_runtime_unavailable",
            (
                "provenance.artifacts.cmf.python names a LOCAL interpreter path only; "
                "a launcher command is not supported because CLIO cannot assume "
                "shell reach into another host. Use server_url for an off-host CMF."
            ),
            configured=value,
        )
    stem = Path(value).stem.lower()
    if stem in _REMOTE_LAUNCHERS:
        raise CMFRefusal(
            "cmf_local_runtime_unavailable",
            (
                f"{stem!r} launches a runtime on another host; "
                "provenance.artifacts.cmf.python names a LOCAL interpreter only. "
                "Use server_url for an off-host CMF."
            ),
            configured=value,
        )
    if platform == "win32":
        raise CMFRefusal(
            "cmf_local_runtime_unsupported_platform",
            "ml-metadata publishes no wheels for win32, so no local CMF runtime "
            "can exist on this host",
            platform=platform,
            configured=value,
        )
    executable = _resolve_python(value)
    if not executable:
        raise CMFRefusal(
            "cmf_local_runtime_unavailable",
            "the configured interpreter does not exist and is not on PATH",
            configured=value,
        )
    return [executable]


def _worker_argv(config: CMFProviderConfig, launcher: list[str]) -> list[str]:
    """Build the full worker argv from a resolved interpreter and the stores.

    Args:
        config: The CMF provider configuration naming the stores.
        launcher: The resolved argv prefix from
            :func:`resolve_local_worker_command`.

    Returns:
        The complete argv for :class:`subprocess.Popen`.
    """
    # Always the bundled worker: local mode runs on THIS filesystem, so there is
    # no second host whose copy of the script would need naming.
    worker = str(Path(__file__).with_name("cmf_worker.py"))
    return [
        *launcher,
        worker,
        "--metadata",
        config.metadata_path.as_posix(),
        "--artifact-root",
        config.artifact_root.as_posix(),
        "--pipeline",
        config.pipeline_name,
    ]


class CMFArtifactStore:
    """CMF provider storage: metadata-only reference or local DVC CAS custody."""

    name = "cmf"

    def __init__(self, config: CMFProviderConfig) -> None:
        self.config = config
        self._lock = threading.Lock()

    def ingest(self, path: Path, *, workspace_root: Path | None) -> IngestedIdentity:
        """Hash by reference or stream once into a DVC-compatible local object store."""
        mode = self.config.artifact_store
        if mode == "reference":
            result = ingest_identity(path, workspace_root=None)
            return replace(result, reason=f"cmf_metadata_reference:{result.reason}")
        if mode != "local":
            raise ValueError(f"unsupported CMF artifact store: {mode}")
        return self._ingest_local(path)

    def _ingest_local(self, path: Path) -> IngestedIdentity:
        stat = path.stat()
        root = self.config.artifact_root
        tmp_root = root / ".tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="ingest-", dir=tmp_root)
        tmp = Path(tmp_name)
        sha256 = hashlib.sha256()
        md5 = hashlib.md5(usedforsecurity=False)
        size = 0
        try:
            with os.fdopen(fd, "wb") as target, open(path, "rb") as source:
                while True:
                    chunk = source.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    target.write(chunk)
                    sha256.update(chunk)
                    md5.update(chunk)
                    size += len(chunk)
            sha_hex = sha256.hexdigest()
            md5_hex = md5.hexdigest()
            object_name = f"{_DVC_OBJECT_PREFIX}{md5_hex[:2]}/{md5_hex[2:]}"
            destination = root / Path(object_name)
            with self._lock:
                destination.parent.mkdir(parents=True, exist_ok=True)
                disposition = "deduplicated" if destination.is_file() else "stored"
                if destination.is_file() and _digest_file(destination, "md5") != md5_hex:
                    disposition = "self_healed"
                    os.replace(tmp, destination)
                elif destination.is_file():
                    tmp.unlink(missing_ok=True)
                else:
                    os.replace(tmp, destination)
        finally:
            tmp.unlink(missing_ok=True)
        receipt = StorageReceipt(
            provider="cmf",
            backend="dvc-local",
            object_uri=f"cmf+dvc://local/{object_name}",
            object_name=object_name,
            size_bytes=size,
            digests={"sha256": sha_hex, "md5": md5_hex},
            disposition=disposition,
        )
        evidence = IdentityEvidence(
            evidence_class=EvidenceClass.HASHED_AT_USE,
            sha256=sha_hex,
            size_bytes=size,
            mtime=float(stat.st_mtime),
            authority=f"cmf:dvc-local:md5:{md5_hex}",
        )
        return IngestedIdentity(
            evidence=evidence,
            custody=Custody.EXTERNAL_REFERENCED,
            reason="cmf_dvc_ingested",
            blob_path=destination,
            storage_receipt=receipt.to_dict(),
        )

    def resolve_owned_path(
        self,
        version: "ArtifactVersion",
        *,
        workspace_root: Path | None,
    ) -> Path | None:
        """Resolve and verify a DVC-local object named by the immutable receipt."""
        del workspace_root
        receipt = (version.producer or {}).get("storage_receipt")
        if not isinstance(receipt, dict) or receipt.get("provider") != "cmf":
            return None
        if receipt.get("backend") != "dvc-local":
            return None
        object_name = str(receipt.get("object_name") or "")
        if not _valid_object_name(object_name):
            return None
        root = self.config.artifact_root.resolve(strict=False)
        path = (root / Path(object_name)).resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError:
            return None
        if not path.is_file():
            return None
        digests = receipt.get("digests")
        expected_md5 = str(digests.get("md5") or "") if isinstance(digests, dict) else ""
        if expected_md5 and _digest_file(path, "md5") != expected_md5:
            return None
        if version.sha256 and _digest_file(path, "sha256") != version.sha256:
            return None
        return path


def _valid_object_name(value: str) -> bool:
    parts = Path(value).parts
    if len(parts) != 4 or parts[:2] != ("files", "md5"):
        return False
    return (
        len(parts[2]) == 2
        and len(parts[3]) == 30
        and all(char in "0123456789abcdef" for char in parts[2] + parts[3])
    )


def _digest_file(path: Path, algorithm: str) -> str:
    digest = hashlib.md5(usedforsecurity=False) if algorithm == "md5" else hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class CMFArtifactProvenanceProvider:
    """Map the artifact substream into CMF-native MLMD entities and events."""

    name = "cmf"
    durable = True
    queryable = True

    def __init__(self, config: CMFProviderConfig, *, bridge: CMFBridge | None = None) -> None:
        self.config = config
        self.store: ArtifactStore = CMFArtifactStore(config)
        self._bridge = bridge or CMFBridge(config)

    def emit(self, event: "SemanticEvent") -> ProviderReceipt:
        """Submit one explicit artifact event to the isolated CMF runtime.

        Reports the worker's OWN outcome. Returning ``ACCEPTED`` for an event
        the worker filtered is what let provider health show 26 accepted, zero
        filtered and zero failed while MLMD received nothing for half of them
        (live qualification, sess_3c2660f69bd5): the counter described the
        hand-off, not the write.
        """
        response = self._bridge.request("record", event=event.to_dict("full"))
        if response.get("filtered"):
            return ProviderReceipt.FILTERED
        if self.config.server_url:
            self._publish()
        return ProviderReceipt.ACCEPTED

    def flush(self) -> None:
        """No-op, and honestly a complete barrier: :meth:`emit` already
        blocks on ``CMFBridge.request``'s synchronous line-delimited
        request/response protocol with the isolated CMF worker (writes
        stdin, reads one response line before returning), so there is
        nothing further to drain by the time emit() has returned."""
        return

    def _publish(self) -> dict[str, Any]:
        """Publish cumulative local MLMD through CMF's server ingestion protocol."""
        return self._bridge.request(
            "publish",
            server_url=self.config.server_url,
            timeout_s=self.config.publish_timeout_s,
        )

    def lineage(
        self,
        artifact_id: str,
        *,
        direction: str,
        depth: int,
        complete: bool = False,
    ) -> dict[str, Any] | None:
        """Query CMF/MLMD and return CLIO's provider-neutral graph shape."""
        response = self._bridge.request(
            "lineage",
            artifact_id=artifact_id,
            direction=direction,
            depth=depth,
            complete=complete,
        )
        graph = response.get("graph")
        return graph if isinstance(graph, dict) else None

    def probe(self) -> dict[str, Any]:
        """Return runtime versions and store paths from the real CMF worker."""
        return self._bridge.request("health")

    def close(self) -> None:
        """Attempt one final cumulative publication, then close the CMF runtime."""
        try:
            if self.config.server_url:
                self._publish()
        finally:
            self._bridge.close()


__all__ = [
    "CMFArtifactProvenanceProvider",
    "CMFArtifactStore",
    "CMFBridge",
    "CMFBridgeError",
    "CMFProviderConfig",
    "resolve_local_worker_command",
]
