"""Standalone Linux supervisor for an owned native service and its installation.

The controller sends a server-generated manifest over stdin. No credential is
written to the manifest or returned in a receipt. Detached workers survive an
SSH disconnect; a boot/start identity prevents signalling a reused PID.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

MARKER = "CLIO_SERVICE_OBSERVATION "


def write_json(path: Path, value: dict[str, Any]) -> None:
    """Publish a private receipt atomically on its owning filesystem."""
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as writer:
        temporary = Path(writer.name)
        json.dump(value, writer)
        writer.flush()
        os.fsync(writer.fileno())
    temporary.replace(path)


def _has_proc() -> bool:
    """Whether this host has a Linux-style /proc (macOS and BSD do not)."""
    return Path("/proc/self/stat").exists()


def identity(pid: int) -> str:
    """Return the exact live process identity, excluding zombies.

    Linux: boot id + start tick from /proc. A POSIX host without /proc (macOS,
    BSD): ``ps``'s start time, which with the PID names one process (F010 /
    DIRECTIVES 11). This supervisor is POSIX-only (process groups, fcntl);
    Windows native services use their own launcher.
    """
    if _has_proc():
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return (
                ""
                if stat[0] == "Z"
                else Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + stat[19]
            )
        except (OSError, IndexError):
            return ""
    return _ps_identity(pid)


def _ps_identity(pid: int) -> str:
    """``ps``-based identity: ``ps:<start time>``, or "" for a gone or zombie process."""
    if pid <= 0:
        return ""
    try:
        done = subprocess.run(
            ["ps", "-o", "stat=", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    fields = done.stdout.strip().split(None, 1)
    if done.returncode != 0 or len(fields) != 2 or fields[0].startswith("Z"):
        return ""
    return "ps:" + " ".join(fields[1].split())


def alive(receipt: dict[str, Any]) -> bool:
    """Require the recorded boot/start identity before treating a PID as owned."""
    return (
        bool(receipt.get("process_identity"))
        and identity(receipt.get("pid", 0)) == receipt["process_identity"]
    )


def checked_root(raw: str) -> Path:
    """Refuse broad, relative or symlinked ownership roots."""
    path = Path(raw)
    if not path.is_absolute() or len(path.parts) < 4 or ".." in path.parts:
        raise ValueError("Choose a dedicated absolute native service directory")
    if path.resolve() != path:
        raise ValueError("Native service directories cannot use symlinked paths")
    return path


@contextmanager
def locked(root: Path) -> Iterator[None]:
    """Serialize lifecycle decisions across clients and detached workers."""
    import fcntl

    with (root / ".lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def owner(root: Path, expected: str) -> None:
    """Only a matching marker grants lifecycle authority over an existing directory."""
    for relative in (
        "owner.json",
        "receipt.json",
        "manifest.json",
        "controller.py",
        "launch.py",
        ".lock",
        "environment",
        "environment/pyproject.toml",
        "environment/.venv",
        "logs",
        "evidence",
        "cache",
        "tmp",
        "credentials.json",
        "settings.yaml",
        "redis.conf",
        "images.json",
        "evidence/verification.json",
    ):
        if (root / relative).is_symlink():
            raise ValueError("A native service ownership path was replaced by a symlink")
    marker = json.loads((root / "owner.json").read_text())
    host = socket.gethostname()
    if marker != {"owner": expected, "host": host, "root": str(root)}:
        if marker.get("root") == str(root) and marker.get("host") not in {None, host}:
            # Shared filesystems (HPC jobs) move a "local" target between hosts (F020).
            raise ValueError(
                f"This native service directory was installed on host {marker['host']}, "
                f"not on this host ({host}); install the service here instead of starting "
                "it (downloaded models are kept)"
            )
        raise ValueError("This native service directory belongs to another deployment or host")


def prepare(root: Path, expected: str) -> None:
    """Claim an empty dedicated directory without adopting existing user files."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with locked(root):
        if not (root / "owner.json").exists():
            if any(path.name != ".lock" for path in root.iterdir()):
                raise ValueError("Refusing to adopt a non-empty native service directory")
            write_json(
                root / "owner.json",
                {"owner": expected, "host": socket.gethostname(), "root": str(root)},
            )
        owner(root, expected)
        root.chmod(0o700)


def hook(root: Path, action: str, *, timeout: int = 90) -> None:
    """Run a definition-owned lifecycle hook and refuse an unsuccessful cleanup."""
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text())
    name = manifest.get("hooks", {}).get(action)
    if not name:
        return
    script = root / name
    if script.parent != root or script.is_symlink() or not script.is_file():
        raise ValueError("Invalid service lifecycle hook")
    completed = subprocess.run(
        [sys.executable, str(script), action],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode:
        raise RuntimeError(f"Service {action} hook failed; inspect retained logs and resources")


def listeners(port: int) -> list[tuple[str, str]]:
    """Every TCP socket listening on ``port`` on this host, as (address, inode).

    On Linux /proc/net lists sockets of every user, so a listener held by
    another account on a shared node is visible even though its process is
    not. Elsewhere a loopback connection attempt is the portable signal.
    """
    found = []
    tables = [Path(table) for table in ("/proc/net/tcp", "/proc/net/tcp6")]
    if not any(table.is_file() for table in tables):
        with socket.socket() as probe:
            probe.settimeout(1)
            return [("127.0.0.1", "")] if probe.connect_ex(("127.0.0.1", port)) == 0 else []
    for table in tables:
        try:
            rows = table.read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            address, _, hex_port = fields[1].partition(":")
            if fields[3] == "0A" and int(hex_port, 16) == port:
                found.append((address, fields[9]))
    return found


def http_get(url: str, key: str = "") -> tuple[int, bytes]:
    """Status and body of a direct (proxy-free) loopback GET; 0 when nothing answered."""
    request = Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, b""
    except (OSError, URLError, ValueError):
        return 0, b""


def serves_identity(root: Path, receipt: dict[str, Any], key: str = "") -> bool:
    """Whether the answering endpoint is provably the owned server (F010, F025).

    Portable across operating systems and native/container runtimes: no socket
    or process-table inspection. A model server must refuse a request without
    the per-launch key and, when the caller holds the key, list the served
    model. A container stack must report every owned component running.
    """
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    check = manifest.get("identity") or {}
    if check.get("kind") == "openai":
        parts = urlsplit(receipt.get("health_url", ""))
        models = f"{parts.scheme}://{parts.netloc}/v1/models"
        if http_get(models)[0] not in {401, 403}:
            return False
        if not key:
            return True
        status, body = http_get(models, key)
        try:
            served = {row.get("id") for row in json.loads(body or b"{}").get("data", [])}
        except (ValueError, AttributeError):
            return False
        return status == 200 and check.get("served_model") in served
    if check.get("kind") == "components":
        script = root / check.get("hook", "")
        if script.parent != root or script.is_symlink() or not script.is_file():
            return False
        try:
            completed = subprocess.run(
                [sys.executable, str(script), "running"],
                cwd=root,
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0
    return False


def read_receipt(root: Path) -> dict[str, Any]:
    """Read the durable receipt, including an interrupted worker's last result."""
    file = root / "receipt.json"
    return (
        json.loads(file.read_text())
        if file.exists()
        else {"installed": False, "phase": "not_installed"}
    )


def effective_artifacts(root: Path) -> dict[str, str]:
    """Keep immutable installed artifacts inspectable after runtime removal."""
    artifacts = dict(read_receipt(root).get("effective_artifacts", {}))
    images = root / "images.json"
    if images.is_file():
        artifacts.update(json.loads(images.read_text()))
    lockfile = root / "environment/uv.lock"
    if lockfile.is_file():
        artifacts["python_lock_sha256"] = hashlib.sha256(lockfile.read_bytes()).hexdigest()
    return artifacts


def observation(root: Path, *, health: bool = True, key: str = "") -> dict[str, Any]:
    """Separate installation, process liveness and actual HTTP serving readiness."""
    receipt = read_receipt(root)
    live = alive(receipt)
    phase = receipt["phase"]
    if not live and phase in {"installing", "running"}:
        phase = "interrupted" if phase == "installing" else "stopped"
    serving = False
    if health and live and phase == "running":
        serving = http_get(receipt["health_url"])[0] == 200 and serves_identity(root, receipt, key)
    verification = root / "evidence/verification.json"
    verified = json.loads(verification.read_text()) if verification.is_file() else {}
    current_verification = verified.get("configuration_revision") == receipt.get(
        "configuration_revision"
    ) and verified.get("generation") == receipt.get("generation")
    return {
        "definition_version": receipt.get("definition_version", "1"),
        "configuration_revision": receipt.get("configuration_revision", ""),
        "phase": phase,
        "installed": bool(receipt.get("installed"))
        and (root / "environment/.venv/bin/python").is_file(),
        "running": live and phase == "running",
        "serving": serving,
        "worker_alive": live,
        "provenance_ingesting": serving
        and current_verification
        and bool(verified.get("provenance_ingesting")),
        "attention_verified": False,
        "error": receipt.get("error"),
        "observed_at": time.time(),
        "evidence_directory": str(root / "evidence"),
        "effective_artifacts": effective_artifacts(root),
    }


def stop(root: Path) -> None:
    """Stop only the exact owned process group and confirm termination."""
    receipt = read_receipt(root)
    if alive(receipt):
        pid = receipt["pid"]
        if os.getpgid(pid) != pid:
            raise ValueError("Recorded native service is not its own process group")
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while alive(receipt) and time.monotonic() < deadline:
            time.sleep(0.1)
        if alive(receipt):
            os.killpg(pid, signal.SIGKILL)
            deadline = time.monotonic() + 5
            while alive(receipt) and time.monotonic() < deadline:
                time.sleep(0.1)
        if alive(receipt):
            raise RuntimeError("Native service did not stop; its data was retained")
    hook(root, "stop")
    receipt.update(
        phase="not_installed" if receipt["phase"] == "not_installed" else "stopped",
        pid=0,
        process_identity="",
        generation=str(uuid.uuid4()),
    )
    write_json(root / "receipt.json", receipt)


def launch(root: Path, request: dict[str, Any]) -> None:
    """Start one supervised install or runtime, idempotently for the same revision."""
    action = request["action"]
    manifest = request["manifest"]
    revision = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    current = read_receipt(root)
    if alive(current):
        wanted = "installing" if action == "install" else "running"
        if current.get("configuration_revision") == revision and current["phase"] == wanted:
            return
        raise ValueError("Stop the current operation before changing native service configuration")
    if action == "start":
        if (
            not observation(root, health=False)["installed"]
            or current.get("configuration_revision") != revision
        ):
            raise ValueError("Install this exact native service configuration before starting it")
        model = manifest.get("model_path")
        if model and not (Path(model).is_absolute() and (Path(model) / "config.json").is_file()):
            raise ValueError("The model directory is unavailable on this execution host")
        # SO_REUSEADDR lets this probe bind beside another user's 0.0.0.0
        # listener, so any existing listener on the port refuses the start.
        if listeners(manifest["port"]):
            raise ValueError(
                f"Port {manifest['port']} already has a listener on this host; choose a free port"
            )
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", manifest["port"]))
    else:
        required = manifest.get("installation_bytes", 20 * 1024**3)
        if shutil.disk_usage(root).free < required:
            raise ValueError(
                f"Insufficient space for runtime installation: requires {required} free bytes"
            )
        environment = root / "environment"
        environment.mkdir(exist_ok=True)
        for name, content in manifest.get("files", {}).items():
            path = root / name
            if (
                path.parent != root
                or path.is_symlink()
                or name in {"owner.json", "receipt.json", "manifest.json", "controller.py", ".lock"}
            ):
                raise ValueError("Invalid service definition support file")
            path.write_text(content, encoding="utf-8")
        write_json(root / "manifest.json", manifest)
        (environment / "pyproject.toml").write_text(manifest["project"], encoding="utf-8")
        (root / "launch.py").write_text(manifest["launcher"], encoding="utf-8")
    (root / "controller.py").write_text(request["script"], encoding="utf-8")
    for name in ("logs", "evidence", "cache", "tmp"):
        (root / name).mkdir(exist_ok=True)
    generation = str(uuid.uuid4())
    env = os.environ.copy()
    # Credentials are ephemeral process environment, never manifest/arguments.
    if request.get("api_key"):
        env["VLLM_API_KEY"] = request["api_key"]
    else:
        env.pop("VLLM_API_KEY", None)
    receipt = {
        **current,
        "phase": "installing" if action == "install" else "running",
        "configuration_revision": revision,
        "definition_version": manifest["definition_version"],
        "health_url": f"http://127.0.0.1:{manifest['port']}{manifest['health_path']}",
        "installed": False if action == "install" else True,
        "generation": generation,
        "operation_id": request["operation_id"],
        "error": None,
    }
    with (root / "logs/supervisor.log").open("a") as logs:
        process = subprocess.Popen(
            [sys.executable, str(root / "controller.py"), "worker", str(root), action, generation],
            stdin=subprocess.DEVNULL,
            stdout=logs,
            stderr=logs,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    receipt.update(pid=process.pid, process_identity=identity(process.pid))
    if not receipt["process_identity"]:
        raise RuntimeError("Native worker failed to establish its process identity")
    write_json(root / "receipt.json", receipt)


def worker_environment(
    root: Path, manifest: dict[str, Any], base: dict[str, str]
) -> dict[str, str]:
    """Isolate the service from CLIO's interpreter and expose its own environment's tools.

    Runtimes JIT-compile with console scripts installed beside their interpreter (vLLM's
    flashinfer runs ``ninja``), so the service environment's ``bin`` leads ``PATH``.
    """
    env = {**base, **manifest.get("environment", {})}
    for name in ("UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"):
        env.pop(name, None)
    env.update(
        UV_CACHE_DIR=str(root / "cache/uv"),
        UV_PYTHON_INSTALL_DIR=str(root / "cache/python"),
        TMPDIR=str(root / "tmp"),
        HF_HOME=str(root / "cache/huggingface"),
        PATH=os.pathsep.join(
            item for item in (str(root / "environment/.venv/bin"), env.get("PATH", "")) if item
        ),
    )
    return env


def copy_output(stream: Any, output: Any, secrets: list[str]) -> None:
    """Copy the server's merged output into its log with credentials redacted."""
    for line in stream:
        for value in secrets:
            line = line.replace(value, "[redacted]")
        output.write(line)
        output.flush()


def group_members(group: int) -> list[int]:
    """Live processes of a POSIX process group other than the caller."""
    if _has_proc():
        pids = [int(entry.name) for entry in Path("/proc").iterdir() if entry.name.isdigit()]
    else:
        pids = _ps_pids()
    members = []
    for pid in pids:
        if pid == os.getpid():
            continue
        try:
            if os.getpgid(pid) == group and identity(pid):
                members.append(pid)
        except OSError:
            continue
    return members


def _ps_pids() -> list[int]:
    """Every PID ``ps`` lists (a host without /proc)."""
    try:
        done = subprocess.run(
            ["ps", "-A", "-o", "pid="], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(word) for word in done.stdout.split() if word.isdigit()]


def release_group(timeout: float = 10) -> None:
    """End descendants left in the worker's own group after its child exited (F014).

    A server's engine processes inherit the output pipe; if the API server dies
    they would keep the GPU and the pipe while nothing supervises them.
    """
    group = os.getpgrp()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in group_members(group):
            try:
                os.kill(pid, sig)
            except OSError:
                continue
        deadline = time.monotonic() + timeout
        while group_members(group) and time.monotonic() < deadline:
            time.sleep(0.1)
        if not group_members(group):
            return


def worker(root: Path, action: str, generation: str) -> None:
    """Run the pinned installer or server while retaining bounded, sanitized logs."""
    # Wait for the parent to commit the receipt under the same lifecycle lock.
    with locked(root):
        receipt = read_receipt(root)
        if receipt.get("generation") != generation:
            return
    stopping = False

    def handle_stop(signum: int, frame: Any) -> None:
        nonlocal stopping
        stopping = True

    # Remain the verifiable group leader until descendants exit. If a child
    # ignores SIGTERM, the controller can still safely kill the exact group.
    signal.signal(signal.SIGTERM, handle_stop)
    manifest = json.loads((root / "manifest.json").read_text())
    env = worker_environment(root, manifest, dict(os.environ))
    if action == "install":
        uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
        command = [
            uv,
            "sync",
            "--no-config",
            "--project",
            str(root / "environment"),
            "--python",
            "3.12",
        ]
    else:
        command = [
            str(root / "environment/.venv/bin/python"),
            str(root / "launch.py"),
            *manifest["arguments"],
        ]
    secret = env.get("VLLM_API_KEY", "")
    secrets = [secret] if secret else []
    credentials = root / "credentials.json"
    if credentials.is_file():
        secrets.extend(
            str(value) for value in json.loads(credentials.read_text()).values() if value
        )
    log = root / "logs" / ("install.log" if action == "install" else "server.log")
    try:
        with subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=root,
            text=True,
            errors="replace",
            bufsize=1,
        ) as process:
            assert process.stdout is not None
            with log.open("w", encoding="utf-8") as output:
                # Wait on the child itself, not on EOF: an orphaned descendant
                # holding the pipe must not hide the server's exit (F014).
                pump = threading.Thread(
                    target=copy_output, args=(process.stdout, output, secrets), daemon=True
                )
                pump.start()
                code = process.wait()
                if not stopping and getattr(os, "getpgrp", None) and os.getpgrp() == os.getpid():
                    release_group()
                pump.join(timeout=10)
                if code == 0 and action == "install" and manifest.get("post_install"):
                    post = root / manifest["post_install"]
                    if post.parent != root or post.is_symlink():
                        raise ValueError("Invalid post-install hook")
                    code = subprocess.run(
                        [str(root / "environment/.venv/bin/python"), str(post), "install"],
                        cwd=root,
                        env=env,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        check=False,
                    ).returncode
        if stopping:
            return
        with locked(root):
            current = read_receipt(root)
            if current.get("generation") != generation:
                return
            if action == "install":
                current.update(installed=code == 0, phase="stopped" if code == 0 else "failed")
                if code == 0:
                    current["effective_artifacts"] = effective_artifacts(root)
            else:
                current.update(phase="stopped" if code == 0 else "failed")
            current.update(
                pid=0,
                process_identity="",
                error=None if code == 0 else f"{action} exited with code {code}; inspect its logs",
            )
            write_json(root / "receipt.json", current)
    except (OSError, ValueError) as exc:
        with locked(root):
            current = read_receipt(root)
            if current.get("generation") == generation:
                current.update(
                    phase="failed",
                    error=f"{action} failed: {type(exc).__name__}",
                    pid=0,
                    process_identity="",
                )
                write_json(root / "receipt.json", current)


def control(request: dict[str, Any]) -> dict[str, Any]:
    """Perform one serialized action and return an observed, sanitized result."""
    root = checked_root(request["root"])
    action = request["action"]
    if action in {"prepare", "install"}:
        prepare(root, request["owner"])
    if not root.exists():
        if action in {"status", "stop", "uninstall", "delete_data"}:
            return {
                "phase": "not_installed",
                "installed": False,
                "running": False,
                "serving": False,
            }
        raise ValueError("Install this native service before managing it")
    try:
        owner(root, request["owner"])
    except ValueError:
        if (
            action == "stop"
            and request.get("require_operation_id")
            and read_receipt(root).get("operation_id") != request["require_operation_id"]
        ):
            # A failed operation's cleanup where that operation launched nothing
            # (its start was refused, e.g. another host owns the directory): there
            # is nothing to undo, so do not raise the same refusal again (F020).
            return {"phase": "untouched", "installed": False, "running": False, "serving": False}
        raise
    with locked(root):
        if action in {"install", "start"}:
            launch(root, request)
        elif action in {"stop", "uninstall"}:
            if (
                request.get("require_operation_id")
                and read_receipt(root).get("operation_id") != request["require_operation_id"]
            ):
                return observation(root)
            stop(root)
            if action == "uninstall":
                hook(root, "uninstall")
                environment = root / "environment"
                if environment.exists():
                    if environment.is_symlink():
                        raise ValueError("Runtime environment path was replaced by a symlink")
                    shutil.rmtree(environment)
                receipt = read_receipt(root)
                receipt.update(installed=False, phase="not_installed")
                write_json(root / "receipt.json", receipt)
        elif action == "delete_data":
            if alive(read_receipt(root)) or observation(root, health=False)["installed"]:
                raise ValueError(
                    "Remove the native runtime before explicitly deleting its retained data"
                )
            hook(root, "delete_data")
            shutil.rmtree(root)
            return {
                "phase": "not_installed",
                "installed": False,
                "running": False,
                "serving": False,
                "data_deleted": True,
            }
        elif action == "verify":
            if not observation(root)["serving"]:
                raise ValueError("Start the service before verifying provenance")
            verify = root / "verify.py"
            if verify.is_symlink() or not verify.is_file():
                raise ValueError("This service definition has no verification procedure")
            receipt = read_receipt(root)
            # Invalidate the previous result before attempting a fresh verification.
            write_json(root / "evidence/verification.json", {})
            process = subprocess.run(
                [str(root / "environment/.venv/bin/python"), str(verify)],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=75,
            )
            if process.returncode:
                diagnostics = process.stderr[-16000:]
                credentials = root / "credentials.json"
                if credentials.is_file():
                    for value in json.loads(credentials.read_text()).values():
                        if value:
                            diagnostics = diagnostics.replace(str(value), "[redacted]")
                (root / "logs/verification.log").write_text(diagnostics)
                raise RuntimeError("Provenance write/readback failed; inspect verification logs")
            evidence = json.loads(process.stdout)
            evidence.update(
                configuration_revision=receipt["configuration_revision"],
                generation=receipt["generation"],
            )
            write_json(root / "evidence/verification.json", evidence)
        elif action == "logs":
            hook(root, "logs")
            tails = []
            for file in (root / "logs").glob("*.log"):
                with file.open("rb") as stream:
                    stream.seek(max(0, file.stat().st_size - 12000))
                    tails.append(file.name + "\n" + stream.read().decode("utf-8", errors="replace"))
            return {"logs": "\n".join(tails)[-16000:]}
        elif action not in {"status", "prepare"}:
            raise ValueError("Unsupported native service action")
        return observation(root, key=request.get("api_key") or "")


if __name__ == "__main__":
    os.umask(0o077)
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        worker(Path(sys.argv[2]), sys.argv[3], sys.argv[4])
    else:
        try:
            result = control(json.load(sys.stdin))
            print(result["logs"] if "logs" in result else MARKER + json.dumps(result))
        except (OSError, ValueError, RuntimeError) as error:
            print(str(error), file=sys.stderr)
            sys.exit(1)
