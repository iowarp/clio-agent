"""Installed native llama.cpp/Ollama installer and launcher; no CLIO imports."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

#: STATUS_DLL_NOT_FOUND (0xC0000135) as Python reports a Windows process's exit status.
WINDOWS_DLL_NOT_FOUND = (0xC0000135, 0xC0000135 - (1 << 32))


def install(root: Path, manifest: dict[str, Any]) -> None:
    """Install verified upstream prebuilt binaries; nothing is compiled on the host."""
    from native_archive import download, extract  # type: ignore[import-not-found]  # noqa: PLC0415

    spec = manifest["native_model"]
    staging = root / "tmp" / "model-runtime"
    if staging.exists():
        if staging.is_symlink() or not staging.resolve().is_relative_to(root.resolve()):
            raise ValueError("Unsafe native runtime staging directory")
        shutil.rmtree(staging)
    staging.mkdir()
    installed = root / "environment" / "runtime"
    if installed.exists():
        if installed.is_symlink() or not installed.resolve().is_relative_to(root.resolve()):
            raise ValueError("Native runtime path is a symlink")
        shutil.rmtree(installed)
    installed.mkdir()
    for index, artifact in enumerate(spec["artifacts"]):
        archive = download(artifact["url"], artifact["sha256"], root / "cache" / "downloads")
        unpacked = extract(
            archive, staging / str(index), compression=artifact.get("compression", "")
        )
        shutil.copytree(unpacked, installed, dirs_exist_ok=True, symlinks=True)
    matches = [path for path in installed.rglob(spec["executable"]) if path.is_file()]
    if len(matches) != 1:
        raise RuntimeError(f"Native runtime did not install one {spec['executable']}")
    executable = matches[0]
    env = binary_environment(root, executable)
    if spec["engine"] == "ollama":
        # --version must never connect to a separately installed user daemon.
        env["OLLAMA_HOST"] = "127.0.0.1:1"
    try:
        version = subprocess.run(
            [str(executable), "--version"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(
            f"The prebuilt {spec['engine']} {spec['backend']} build does not run on this host: "
            f"{refusal_detail(error)}. CLIO does not compile it; use the container variant."
        ) from error
    print((version.stdout + version.stderr).strip(), flush=True)
    if spec["engine"] == "llama_cpp" and spec["backend"] != "cpu":
        devices = subprocess.run(
            [str(executable), "--list-devices"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        wanted = "rocm" if spec["backend"] == "rocm" else spec["backend"]
        if wanted not in devices.stdout.lower():
            raise RuntimeError(
                f"The selected native {wanted} backend cannot see a GPU; check its host driver/runtime"
            )
        print(devices.stdout, flush=True)
    (root / "native-binary.json").write_text(
        json.dumps({"executable": str(executable.relative_to(root))}), encoding="utf-8"
    )


def refusal_detail(error: OSError | subprocess.CalledProcessError) -> str:
    """Say why a fetched binary could not start, including Windows' silent DLL failure."""
    if getattr(error, "returncode", None) in WINDOWS_DLL_NOT_FOUND:
        # Windows reports a missing DLL only through this status, with no output.
        return (
            "a required DLL is missing; install the Microsoft Visual C++ 2015-2022 "
            "Redistributable (x64)"
        )
    output = getattr(error, "stderr", None) or getattr(error, "stdout", None) or str(error)
    return output.strip()[:400]


def binary_environment(root: Path, executable: Path) -> dict[str, str]:
    """Load this runtime's accompanying libraries before host libraries."""
    env = os.environ.copy()
    env["PATH"] = str(executable.parent) + os.pathsep + env.get("PATH", "")
    if os.name != "nt":
        env["LD_LIBRARY_PATH"] = os.pathsep.join(
            [
                str(executable.parent),
                str(root / "environment/runtime/lib"),
                str(root / "environment/runtime/lib64"),
                # NVIDIA's CUDA runtime wheels (Linux CUDA build), unpacked beside it.
                *sorted(str(path) for path in (root / "environment/runtime").glob("nvidia/*/lib")),
                env.get("LD_LIBRARY_PATH", ""),
            ]
        )
    return env


def serve(root: Path, manifest: dict[str, Any]) -> None:
    """Run the exact owned executable and model with inherited ephemeral credentials."""
    spec = manifest["native_model"]
    launch = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
    binary = json.loads((root / "native-binary.json").read_text(encoding="utf-8"))
    executable = root / binary["executable"]
    if not executable.resolve().is_relative_to((root / "environment").resolve()):
        raise ValueError("Native runtime executable escapes its owned environment")
    env = binary_environment(root, executable)
    env.update(launch.get("environment", {}))
    cache = Path(launch["model_cache"])
    cache.mkdir(parents=True, exist_ok=True)
    env["LLAMA_CACHE"] = str(cache)
    # Runtime libraries shipped beside the executable retain their relative lookup.
    env["PATH"] = str(executable.parent) + os.pathsep + env.get("PATH", "")
    if spec["engine"] == "ollama":
        env.update(OLLAMA_HOST=f"127.0.0.1:{manifest['port']}", OLLAMA_MODELS=str(cache))
        if spec["backend"] == "cpu":
            env.update(
                CUDA_VISIBLE_DEVICES="-1", ROCR_VISIBLE_DEVICES="-1", GGML_VK_VISIBLE_DEVICES="-1"
            )
        if spec["backend"] == "vulkan":
            env["OLLAMA_VULKAN"] = "1"
        ready = root / "runtime-ready.json"
        if ready.is_symlink():
            raise ValueError("Native model readiness cannot be a symlink")
        ready.unlink(missing_ok=True)
        with subprocess.Popen([str(executable), "serve"], env=env) as process:
            try:
                deadline = time.monotonic() + 90
                while process.poll() is None:
                    try:
                        _ollama(manifest["port"], "/api/version")
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Native Ollama did not become ready") from None
                        time.sleep(0.2)
                if process.poll() is not None:
                    raise RuntimeError("Native Ollama exited before becoming ready")
                model = launch.get("model")
                if model:
                    try:
                        _ollama(manifest["port"], "/api/show", {"model": model})
                    except HTTPError as error:
                        if error.code != 404:
                            raise
                        subprocess.run([str(executable), "pull", model], env=env, check=True)
                    options = {}
                    if launch.get("context_length"):
                        options["num_ctx"] = int(launch["context_length"])
                    _ollama(
                        manifest["port"],
                        "/api/generate",
                        {
                            "model": model,
                            "prompt": "",
                            "stream": False,
                            "options": options,
                            "keep_alive": 0,
                        },
                    )
                ready.write_text(json.dumps({"model": model}), encoding="utf-8")
                if process.wait():
                    raise RuntimeError("Native Ollama server exited unsuccessfully")
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        return
    else:
        arguments = ["--host", "127.0.0.1", "--port", str(manifest["port"]), *launch["flags"]]
    subprocess.run([str(executable), *arguments], env=env, check=True)


def _ollama(port: int, path: str, payload: dict[str, Any] | None = None) -> Any:
    """Call the owned native Ollama API, including long model initialization."""
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with build_opener(ProxyHandler({})).open(request, timeout=600) as response:
        return json.load(response)


def running(root: Path, manifest: dict[str, Any]) -> None:
    """Confirm the real engine API and, for llama.cpp, the requested served model."""
    launch = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
    engine = manifest["native_model"]["engine"]
    path = "/api/version" if engine == "ollama" else "/v1/models"
    headers = (
        {"Authorization": "Bearer " + os.environ["LLAMA_API_KEY"]}
        if os.environ.get("LLAMA_API_KEY")
        else {}
    )
    request = Request(f"http://127.0.0.1:{manifest['port']}{path}", headers=headers)
    with build_opener(ProxyHandler({})).open(request, timeout=10) as response:
        payload = json.load(response)
    if engine == "ollama":
        if not payload.get("version"):
            raise RuntimeError("Ollama did not report its version")
        ready = root / "runtime-ready.json"
        if not ready.is_file() or json.loads(ready.read_text()).get("model") != launch.get("model"):
            raise RuntimeError("Ollama is still preparing the requested model")
    elif launch["served_model"] not in {row.get("id") for row in payload.get("data", [])}:
        raise RuntimeError("llama.cpp is not serving the requested model")


def main() -> None:
    """Dispatch only the definition-owned hook actions."""
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    action = sys.argv[1]
    if action == "install":
        install(root, manifest)
    elif action == "serve":
        serve(root, manifest)
    elif action == "running":
        running(root, manifest)
    else:
        raise ValueError("Unsupported native model hook action")


if __name__ == "__main__":
    main()
