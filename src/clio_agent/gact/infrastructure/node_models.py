"""Target-side model jobs; executed on the selected Linux host, never its controller.

This file is standalone so the host need not already have CLIO installed.
Acquisition runs in an owned uv project with a pinned Hugging Face client.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

# The worker's owned uv project installs only this; its imports must stay within it and stdlib.
WORKER_DEPENDENCIES = ("huggingface-hub==0.35.3",)


class AcquisitionError(ValueError):
    """An explicitly sanitized failure safe to display outside the execution host."""


def write_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically publish receipts on the same filesystem."""
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as writer:
        temporary = Path(writer.name)
        json.dump(value, writer)
        writer.flush()
        os.fsync(writer.fileno())
    temporary.replace(path)


def process_identity(pid: int) -> str:
    """Identify the exact Linux process, including boot and start time; reject zombies."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return ""
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + stat[19]
    except (OSError, IndexError):
        return ""


def alive(job: dict[str, Any]) -> bool:
    """A PID alone does not establish ownership after restart or PID reuse."""
    return (
        bool(job.get("process_identity"))
        and process_identity(job.get("pid", 0)) == job["process_identity"]
    )


def public(job: dict[str, Any]) -> dict[str, Any]:
    """Return status without execution internals or credentials."""
    return {
        key: value
        for key, value in job.items()
        if key not in {"pid", "process_identity", "verified_files", "force_redownload"}
    }


def complete(job: dict[str, Any]) -> bool:
    """Check that every verified file is still present and unchanged since verification."""
    root = Path(job["destination"])
    if not job.get("verified_files"):
        return False
    for relative, stamp in job["verified_files"].items():
        path = root / relative
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                return False
            observed = path.stat()
            if [observed.st_size, observed.st_mtime_ns] != stamp:
                return False
        except OSError:
            return False
    return True


def inspect_jobs(root: Path) -> list[dict[str, Any]]:
    """Reconcile dead workers without claiming an incomplete model is reusable."""
    if not root.is_dir():
        raise ValueError("The recorded model storage location is unavailable on this host")
    rows = []
    for path in sorted(root.glob("*/receipt.json")):
        job = json.loads(path.read_text())
        if job["state"] in {"queued", "running"} and not alive(job):
            job.update(
                state="interrupted",
                error="The download process ended; retry to reuse its cached bytes.",
            )
        if job["state"] == "ready" and not complete(job):
            job.update(
                state="stale",
                error="Model files changed or disappeared; retry to verify the revision.",
            )
        rows.append(public(job))
    return rows


def start(root: Path, request: dict[str, Any], script: str) -> dict[str, Any]:
    """Start or reuse one durable acquisition, with a target-owned process and cache."""
    import fcntl

    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
    if not Path(uv).is_file():
        raise ValueError("Install uv on this execution host before downloading models")
    repository, revision = request["repository"], request["revision"]
    if not re.fullmatch(r"[A-Za-z0-9][\w.-]*(?:/[A-Za-z0-9][\w.-]*)?", repository):
        raise ValueError("Invalid model repository")
    if not re.fullmatch(r"[\w./-]+", revision) or ".." in revision:
        raise ValueError("Invalid model revision")
    destination = Path(request["destination"])
    if (
        not destination.is_absolute()
        or ".." in destination.parts
        or destination == Path(destination.anchor)
    ):
        raise ValueError("Select a dedicated absolute model directory on this host")
    destination = destination.resolve()
    files = sorted(set(request.get("files") or []))
    for name in files:
        if not re.fullmatch(r"[\w.+-]+(?:/[\w.+-]+)*", name) or ".." in name.split("/"):
            raise ValueError("Invalid model file name")
    identity = f"{repository}\0{revision}\0{destination}"
    if files:
        identity += "\0" + "\0".join(files)
    job_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
    folder = root / job_id
    folder.mkdir(parents=True, exist_ok=True)
    with (root / ".control.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = folder / "receipt.json"
        previous = None
        if receipt.exists():
            previous = json.loads(receipt.read_text())
            if (previous["state"] == "ready" and complete(previous)) or alive(previous):
                return public(previous)
        for path in root.glob("*/receipt.json"):
            other = json.loads(path.read_text())
            if other["id"] != job_id and Path(other["destination"]) == destination:
                raise ValueError(
                    "This destination belongs to a different model revision; choose another directory"
                )
        marker = destination / ".clio-model.json"
        if destination.exists() and not marker.exists() and any(destination.iterdir()):
            raise ValueError("The model destination is not empty and has no CLIO ownership receipt")
        destination.mkdir(parents=True, exist_ok=True)
        if marker.exists():
            owner = json.loads(marker.read_text())
            if owner.get("job_id") != job_id or owner.get("operation_root", str(root)) != str(root):
                raise ValueError(
                    "This model directory belongs to another acquisition or storage root"
                )
        write_json(
            marker,
            {
                "job_id": job_id,
                "repository": repository,
                "requested_revision": revision,
                "operation_root": str(root),
            },
        )
        worker = folder / "download.py"
        worker.write_text(script)
        (folder / "pyproject.toml").write_text(
            '[project]\nname="clio-model-download"\nversion="1.0.0"\nrequires-python=">=3.11"\n'
            f"dependencies={json.dumps(list(WORKER_DEPENDENCIES))}\n"
        )
        if any(path.is_symlink() for path in destination.rglob("*")):
            raise ValueError("Model directories must not contain symbolic links")
        job = {
            "id": job_id,
            "repository": repository,
            "requested_revision": revision,
            "revision": previous.get("revision") if previous else request.get("resolved_revision"),
            "destination": str(destination),
            "files": files,
            "file_path": str(destination / files[0]) if len(files) == 1 else None,
            "state": "queued",
            "phase": "Preparing download tools",
            "bytes_done": 0,
            "bytes_total": None,
            "created_at": previous["created_at"] if previous else time.time(),
            "updated_at": time.time(),
            "error": None,
            "force_redownload": bool(
                previous and (previous.get("force_redownload") or previous["state"] == "ready")
            ),
        }
        write_json(receipt, job)
        (folder / "cancel").unlink(missing_ok=True)
        env = dict(
            os.environ,
            UV_CACHE_DIR=str(root / "uv-cache"),
            UV_PYTHON_INSTALL_DIR=str(root / "python"),
            UV_PROJECT_ENVIRONMENT=str(root / "environment"),
            HF_HUB_DISABLE_XET="1",
            HF_HUB_DISABLE_TELEMETRY="1",
        )
        with (folder / "download.log").open("ab") as log:
            process = subprocess.Popen(
                [
                    uv,
                    "run",
                    "--project",
                    str(folder),
                    "--python",
                    "3.12",
                    str(worker),
                    "--worker",
                    str(receipt),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                env=env,
                start_new_session=True,
            )
        job.update(pid=process.pid, process_identity=process_identity(process.pid))
        write_json(receipt, job)
        return public(job)


def cancel(root: Path, job_id: str) -> dict[str, Any]:
    """Stop only the recorded process group, keeping partial and complete model files."""
    import fcntl

    with (root / ".control.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _cancel_locked(root, job_id)


def _cancel_locked(root: Path, job_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{24}", job_id):
        raise ValueError("Invalid model operation")
    folder = root / job_id
    path = folder / "receipt.json"
    job = json.loads(path.read_text())
    if job["state"] == "ready":
        return public(job)
    (folder / "cancel").touch()
    if alive(job):
        os.killpg(job["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 10
        while alive(job) and time.monotonic() < deadline:
            time.sleep(0.1)
        if alive(job):
            raise RuntimeError("Cancellation requested; worker has not stopped yet")
    job.update(state="cancelled", phase="Cancelled; cached bytes retained", updated_at=time.time())
    write_json(path, job)
    return public(job)


def download(receipt: Path) -> None:
    """Download a resolved revision and verify file sizes/hashes before marking it ready."""
    import threading

    from huggingface_hub import HfApi, snapshot_download

    # Parent publishes process identity first; both processes write the receipt.
    job = json.loads(receipt.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = json.loads(receipt.read_text())
        if job.get("process_identity"):
            break
        time.sleep(0.02)

    try:
        job.update(state="running", phase="Resolving immutable model revision")
        write_json(receipt, job)
        info = HfApi().model_info(
            job["repository"],
            revision=job.get("revision") or job["requested_revision"],
            files_metadata=True,
        )
        if not info.sha or not re.fullmatch(r"[a-f0-9]{40,64}", info.sha):
            raise AcquisitionError("Registry did not return an immutable model revision")
        siblings = info.siblings or []
        if not siblings or any(row.size is None for row in siblings):
            raise AcquisitionError(
                "Registry did not provide the file sizes needed for a capacity check"
            )
        if job.get("files"):
            available = {row.rfilename for row in siblings}
            missing = [name for name in job["files"] if name not in available]
            if missing:
                raise AcquisitionError(f"This model revision has no file named {missing[0]!r}")
            siblings = [row for row in siblings if row.rfilename in set(job["files"])]
        for row in siblings:
            relative = PurePosixPath(row.rfilename)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or "\\" in row.rfilename
                or relative.parts[0] in {".clio-model.json", ".cache"}
            ):
                raise AcquisitionError("Registry contains a reserved or unsafe model file path")
        total = sum(row.size or 0 for row in siblings)
        destination = Path(job["destination"])
        job.update(revision=info.sha, bytes_total=total, phase="Downloading model files")
        # Existing verified/cache bytes are reusable; avoid charging their size twice.
        present = sum(
            path.stat().st_size
            for path in destination.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        if shutil.disk_usage(destination).free < max(0, total - present) + 64 * 1024 * 1024:
            raise AcquisitionError("Insufficient space on the selected model filesystem")
        write_json(receipt, job)
        # The pinned SDK does not pass snapshot tqdm_class to individual files.
        # Observe actual cached bytes instead of reporting fictitious network progress.
        stopped = threading.Event()

        def observe() -> None:
            while not stopped.wait(0.5):
                paths = [destination / row.rfilename for row in siblings]
                paths.extend((destination / ".cache/huggingface/download").rglob("*.incomplete"))
                sizes = 0
                for path in paths:
                    try:
                        if path.is_file() and not path.is_symlink():
                            sizes += path.stat().st_size
                    except FileNotFoundError:
                        pass  # SDK atomically renamed a completed download.
                job.update(bytes_done=min(total, sizes), updated_at=time.time())
                write_json(receipt, job)

        observer = threading.Thread(target=observe, daemon=True)
        observer.start()
        try:
            snapshot_download(
                job["repository"],
                revision=info.sha,
                local_dir=destination,
                max_workers=4,
                force_download=job.get("force_redownload", False),
                allow_patterns=job.get("files") or None,
            )
        finally:
            stopped.set()
            observer.join()
        job.update(phase="Verifying downloaded revision")
        write_json(receipt, job)
        verified = {}
        for row in siblings:
            path = destination / row.rfilename
            if (
                path.is_symlink()
                or not path.resolve().is_relative_to(destination.resolve())
                or not path.is_file()
            ):
                raise AcquisitionError("Downloaded model contains an invalid file path")
            if row.size is not None and path.stat().st_size != row.size:
                raise AcquisitionError("Downloaded model file size differs from registry metadata")
            digest = getattr(row.lfs, "sha256", None) if row.lfs else None
            if digest:
                with path.open("rb") as reader:
                    actual = hashlib.file_digest(reader, "sha256").hexdigest()
                if actual != digest:
                    raise AcquisitionError("Downloaded model file failed its registry hash check")
            elif row.blob_id:
                hasher = hashlib.sha1(
                    f"blob {path.stat().st_size}\0".encode(), usedforsecurity=False
                )
                with path.open("rb") as reader:
                    while chunk := reader.read(1024 * 1024):
                        hasher.update(chunk)
                if hasher.hexdigest() != row.blob_id:
                    raise AcquisitionError("Downloaded file failed its Git blob hash check")
            verified[row.rfilename] = [path.stat().st_size, path.stat().st_mtime_ns]
        job["verified_files"] = verified
        job.update(
            state="ready",
            force_redownload=False,
            phase="Model available; choose a runtime to serve it",
            bytes_done=total,
            updated_at=time.time(),
        )
        write_json(receipt, job)
    # The pinned client's HTTP errors derive from requests' RequestException, an OSError.
    except (OSError, ValueError, RuntimeError) as exc:
        # Upstream exception strings can contain signed URLs or auth headers.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = (
            str(exc)
            if isinstance(exc, AcquisitionError)
            else (
                "Registry access denied; authorize this host for the model and retry"
                if status in {401, 403}
                else f"Model acquisition failed ({type(exc).__name__}); retry to reuse cached bytes"
            )
        )
        job.update(
            state="failed",
            error=detail,
            updated_at=time.time(),
            force_redownload=job["phase"] == "Verifying downloaded revision",
        )
        write_json(receipt, job)
        # Fail the detached process loudly without exposing upstream URLs or credentials.
        raise AcquisitionError(detail) from None


def main() -> None:
    """Dispatch an infrastructure request delivered through the selected target transport."""
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        download(Path(sys.argv[2]))
        return
    request = json.loads(sys.stdin.read())
    root = Path(request["root"])
    if not root.is_absolute() or root == Path(root.anchor) or ".." in root.parts:
        raise ValueError("Model operations require an absolute owned storage root")
    hostname = re.sub(r"[^A-Za-z0-9_.-]", "_", os.uname().nodename)
    root = root.resolve() / "model-operations" / hostname
    action = request["action"]
    if action == "list":
        result: Any = inspect_jobs(root)
    elif action == "start":
        result = start(root, request, request["script"])
    elif action == "cancel":
        result = cancel(root, request["id"])
    else:
        raise ValueError("Unsupported model operation")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
