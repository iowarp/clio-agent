"""Host-specific, container-free model runtimes on the shared native supervisor."""

from __future__ import annotations

import hashlib
import json
import ntpath
import posixpath
from dataclasses import replace
from pathlib import Path
from typing import Any

from clio_agent.gact.infrastructure.context_sizing.deployment import sizing_request
from clio_agent.gact.infrastructure.models import InfrastructureTarget, ServiceVariant, TargetFacts
from clio_agent.gact.infrastructure.ollama_context_apply import ollama_context_hook
from clio_agent.gact.infrastructure.plan import DriverPlan
from clio_agent.gact.infrastructure.server_parameters import EngineId, compile_parameters
from clio_agent.gact.infrastructure.service_paths import service_directory
from clio_agent.gact.infrastructure.storage import resolved_locations
from clio_agent.gact.infrastructure.supervised_service import supervised_plan

LLAMA_BUILD = "b11206"
OLLAMA_VERSION = "0.34.4"
LLAMA_ASSETS = {
    ("windows", "x86_64", "cpu"): (
        "win-cpu-x64.zip",
        "c17f1e3233fc5f5b8915472affa939adee0c95785882503b106d5b14aba01002",
    ),
    ("windows", "aarch64", "cpu"): (
        "win-cpu-arm64.zip",
        "c92b84ad5bcc0c536630706f46c594e25a97c934cdb3f6ddd5a5e36e4e15b186",
    ),
    ("windows", "x86_64", "vulkan"): (
        "win-vulkan-x64.zip",
        "14585588e0dd15788d611ef208a42f77eb76b04f02c6857b57ed4d45aefb8509",
    ),
    ("windows", "x86_64", "rocm"): (
        "win-rocm-10.0-x64.zip",
        "006b735dbc66d7c003ec0dba6d56a8fdac71d692642e28ec7706bc626fcab557",
    ),
    ("windows", "x86_64", "cuda"): (
        "win-cuda-12.4-x64.zip",
        "8327474c51ac8f830e6e0abf15707267fb39cc130d5700643a16e5f7b5451c62",
    ),
    # Upstream's Ubuntu builds (glibc >= 2.34); nothing is compiled on the host.
    ("linux", "x86_64", "cpu"): (
        "ubuntu-x64.tar.gz",
        "aea9ff64167ea473bf5cf463f07b42beac16c857cdffce57c7ed9cc323ea4da5",
    ),
    ("linux", "aarch64", "cpu"): (
        "ubuntu-arm64.tar.gz",
        "695a52577845f513e3fcceac4792dab62858a6aaf1e8e67054279b6b84e9ec23",
    ),
    ("linux", "x86_64", "vulkan"): (
        "ubuntu-vulkan-x64.tar.gz",
        "82475f17280905296548066d864e08f65b995a0caad5d61e3752539ed584ebe6",
    ),
    ("linux", "aarch64", "vulkan"): (
        "ubuntu-vulkan-arm64.tar.gz",
        "721cd21e2498a7305ecb8636a67b235954dcb3a1e67c3a3af9e3f9a9243da162",
    ),
    ("linux", "x86_64", "cuda"): (
        "ubuntu-cuda-12.8-x64.tar.gz",
        "fa78d7d80b8dca117638c49fc4aa58d6b01407541804483876c27323ebb887de",
    ),
    ("macos", "x86_64", "cpu"): (
        "macos-x64.tar.gz",
        "9a8d8df869373d2ae3db63ae8cf80be1aec643d435c1c19aa5c927a5d0f8f873",
    ),
    ("macos", "aarch64", "metal"): (
        "macos-arm64.tar.gz",
        "a93cca3e3c86fc3430b036b56ba22a74a335893e7f930cc5a81314c6df4a5174",
    ),
}
# The Linux CUDA build links the CUDA 12 runtime it does not bundle: NVIDIA's own wheels.
LINUX_CUDA_RUNTIME = (
    (
        "https://files.pythonhosted.org/packages/0d/9b/a997b638fcd068ad6e4d53b8551a7d30fe8b404d6f1804abf1df69838932/nvidia_cuda_runtime_cu12-12.8.90-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
        "adade8dcbd0edf427b7204d480d6066d33902cab2a4707dcfc48a2d0fd44ab90",
    ),
    (
        "https://files.pythonhosted.org/packages/dc/61/e24b560ab2e2eaeb3c839129175fb330dfcfc29e5203196e5541a4c44682/nvidia_cublas_cu12-12.8.4.1-py3-none-manylinux_2_27_x86_64.whl",
        "8ac4e771d5a348c551b2a426eda6193c19aa630236b418086020df5ba9667142",
    ),
)
OLLAMA_ASSETS = {
    ("windows", "x86_64"): (
        "windows-amd64.zip",
        "535193f38f3344e5b08f5d1c171c31ce11aa17f0124ff69ae26d8ec7fe06fa62",
    ),
    ("windows", "aarch64"): (
        "windows-arm64.zip",
        "b49aa49306da9bae5b7498f320f76bd2a13739c3105bcb6e115a26f0dfd10d67",
    ),
    ("linux", "x86_64"): (
        "linux-amd64.tar.zst",
        "c238986e61d40c0cc5f4a9b9e40b9eea104350b77efa34741fc134e105cb9533",
    ),
    ("linux", "aarch64"): (
        "linux-arm64.tar.zst",
        "96f50a1192133028cf4e010d8c333f8af14b1505db6be7b2034c11487e7fd7e6",
    ),
    ("macos", "x86_64"): (
        "darwin.tgz",
        "e9c8fddaab5f48f47f2c4ae3d23d0732f5182417125353faeed2188e34a22799",
    ),
    ("macos", "aarch64"): (
        "darwin.tgz",
        "e9c8fddaab5f48f47f2c4ae3d23d0732f5182417125353faeed2188e34a22799",
    ),
}
ROCM_ASSETS = {
    "windows": (
        "windows-amd64-rocm.zip",
        "73b02b93f4d9335e29c467e54004fcd340db6efc0fb7f50dfef17597327c616b",
    ),
    "linux": (
        "linux-amd64-rocm.tar.zst",
        "dbfcbdff6c36f69bfed177d1807aa2a23ad04ad9208f9a3f648d2fd36136ff3d",
    ),
}
BACKENDS = {
    "cpu": "CPU",
    "vulkan": "Vulkan (AMD, Intel or NVIDIA)",
    "rocm": "AMD ROCm",
    "cuda": "NVIDIA CUDA",
    "metal": "Apple Metal",
}
PROFILE = "native-model-1"


def native_model_variants(engine: str, facts: TargetFacts) -> list[ServiceVariant]:
    """Offer the native backends supported by this OS/architecture and observed GPU."""
    result = []
    backends = (
        ["metal"]
        if facts.os == "macos" and facts.arch == "aarch64"
        else ["cpu", "vulkan", "rocm", "cuda"]
    )
    for backend in backends:
        available = (
            (facts.os, facts.arch, backend) in LLAMA_ASSETS
            if engine == "llama_cpp"
            else (facts.os, facts.arch) in OLLAMA_ASSETS
        )
        reason = ""
        if not available or facts.arch not in {"x86_64", "aarch64"}:
            reason = (
                "No upstream prebuilt build for this OS, architecture and backend; "
                "CLIO does not compile it. Use the container variant."
            )
        elif facts.os == "windows" and facts.target_id != "local":
            reason = "Native Windows installation currently runs beside the active CLIO."
        elif not facts.uv_available:
            reason = "Install uv on this execution host, then inspect again."
        elif backend == "rocm" and (facts.accelerator != "amd" or facts.arch != "x86_64"):
            reason = "Requires an AMD GPU on x86-64."
        elif backend == "cuda" and facts.accelerator != "nvidia":
            reason = "Requires an NVIDIA GPU."
        elif backend == "vulkan" and facts.accelerator not in {"amd", "nvidia", "intel"}:
            reason = "Requires a GPU with a Vulkan driver."
        elif facts.os == "macos" and backend not in {"cpu", "metal"}:
            reason = "Use the native CPU or Apple Metal build on macOS."
        result.append(
            ServiceVariant(
                id=f"native-{facts.os}-{backend}",
                label=BACKENDS[backend] + " (native)",
                version=LLAMA_BUILD if engine == "llama_cpp" else OLLAMA_VERSION,
                install_type="native_uv",
                artifact=f"{engine}:{backend}",
                compatible=not reason,
                reason=reason or "Native installation; no container runtime is needed.",
            )
        )
    # Windows Radeon drivers expose Vulkan directly; Linux compute hosts use ROCm.
    preferred = (
        "rocm"
        if facts.accelerator == "amd" and facts.os == "linux"
        else "vulkan"
        if facts.accelerator in {"amd", "intel"}
        else "cuda"
        if facts.accelerator == "nvidia"
        else "metal"
        if "metal" in backends
        else "cpu"
    )
    result.sort(key=lambda row: (not row.id.endswith("-" + preferred), not row.compatible))
    return result


def native_model_plan(
    engine: EngineId,
    service_name: str,
    action: str,
    variant: str,
    configuration: dict[str, str],
    facts: TargetFacts,
    target: InfrastructureTarget | None,
    api_key: str | None,
) -> DriverPlan:
    """Prepare a native runtime separately from its model, using durable process ownership."""
    candidates = native_model_variants(engine, facts)
    selected = next((row for row in candidates if row.id == variant), None)
    if selected is None:
        raise ValueError(f"Unknown native {engine} variant {variant}")
    if action in {"install", "reinstall", "start"} and not selected.compatible:
        raise ValueError(selected.reason)
    backend = variant.rsplit("-", 1)[-1]
    artifacts: list[dict[str, str]] = []
    if engine == "llama_cpp":
        suffix, checksum = LLAMA_ASSETS[facts.os, facts.arch, backend]
        artifacts.append(
            {
                "url": f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}/llama-{LLAMA_BUILD}-bin-{suffix}",
                "sha256": checksum,
            }
        )
        if backend == "cuda" and facts.os == "linux":
            artifacts.extend({"url": url, "sha256": sha} for url, sha in LINUX_CUDA_RUNTIME)
        elif backend == "cuda":
            artifacts.append(
                {
                    "url": f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}/cudart-llama-bin-win-cuda-12.4-x64.zip",
                    "sha256": "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6",
                }
            )
    else:
        suffix, checksum = OLLAMA_ASSETS[facts.os, facts.arch]
        artifacts.append(
            {
                "url": f"https://github.com/ollama/ollama/releases/download/v{OLLAMA_VERSION}/ollama-{suffix}",
                "sha256": checksum,
                "compression": "zstd" if suffix.endswith(".zst") else "",
            }
        )
        if backend == "rocm":
            suffix, checksum = ROCM_ASSETS[facts.os]
            artifacts.append(
                {
                    "url": f"https://github.com/ollama/ollama/releases/download/v{OLLAMA_VERSION}/ollama-{suffix}",
                    "sha256": checksum,
                    "compression": "zstd" if suffix.endswith(".zst") else "",
                }
            )
    windows = facts.os == "windows"
    paths = ntpath if windows else posixpath
    directory = configuration.get("storage.service_directory") or service_directory(
        service_name, facts, target
    )
    model_cache = configuration.get("storage.model_cache") or (
        paths.join(resolved_locations(target, facts).models, service_name)
        if target and any(target.storage.model_dump().values())
        else paths.join(directory, "data", "models")
    )
    if not paths.isabs(model_cache) or any(ord(character) < 32 for character in model_cache):
        raise ValueError("Model cache must be an absolute path on the execution host")
    port = int(configuration.get("port") or ("8088" if engine == "llama_cpp" else "11434"))
    if not 1024 <= port <= 65535:
        raise ValueError("Port must be between 1024 and 65535")
    ownership = (
        configuration.get("native_owner")
        or hashlib.sha256(f"{facts.target_id}:{facts.hostname}:{directory}".encode()).hexdigest()
    )
    flags: list[str] = []
    served = ""
    compiled = compile_parameters(engine, backend, configuration) if action == "start" else None
    if action == "start" and engine == "llama_cpp":
        model = configuration.get("model_path", "").strip()
        hf = configuration.get("hf_model", "").strip()
        if bool(model) == bool(hf):
            raise ValueError(
                "Choose exactly one GGUF model path or Hugging Face GGUF before starting"
            )
        if any(
            character in value for value in (model, hf) for character in ("\0", "\r", "\n")
        ) or hf.startswith("-"):
            raise ValueError(
                "Model references cannot contain control characters or option prefixes"
            )
        if model and not paths.isabs(model):
            raise ValueError("GGUF model path must be absolute on the execution host")
        served = configuration.get("served_model_name") or (paths.basename(model) if model else hf)
        if served.startswith("-") or any(character in served for character in ("\0", "\r", "\n")):
            raise ValueError("Served model name is not valid")
        flags = ["-m", model] if model else ["-hf", hf]
        flags += ["--alias", served, *(compiled.flags if compiled else ())]
        if backend != "cpu" and not configuration.get("param.gpu_layers"):
            flags += ["--n-gpu-layers", "999"]
    dependencies = ["zstandard==0.25.0"]
    manifest: dict[str, Any] = {
        "service": engine,
        "definition_version": PROFILE,
        "project": '[project]\nname="clio-native-model"\nversion="0.0.0"\nrequires-python=">=3.12,<3.13"\ndependencies='
        + json.dumps(dependencies)
        + "\n",
        "launcher": "from native_model_hook import main\nimport sys\nsys.argv=['launch','serve']\nmain()\n",
        "files": {
            name: Path(__file__).with_name(name).read_text(encoding="utf-8")
            for name in ("native_model_hook.py", "native_archive.py")
        },
        "post_install": "native_model_hook.py",
        "arguments": [],
        "installation_bytes": 6 * 1024**3,
        "port": port,
        "health_path": "/health" if engine == "llama_cpp" else "/api/version",
        "identity": {"kind": "openai", "served_models_file": "runtime-models.json"}
        if api_key
        else {"kind": "components", "hook": "native_model_hook.py"},
        "runtime_fields": ["port", "identity"],
        "native_model": {
            "engine": engine,
            "backend": backend,
            "artifacts": artifacts,
            "executable": ("llama-server" if engine == "llama_cpp" else "ollama")
            + (".exe" if windows else ""),
        },
    }
    plan = supervised_plan(
        action,
        directory=directory,
        ownership=ownership,
        manifest=manifest,
        port=port,
        label="llama.cpp" if engine == "llama_cpp" else "Ollama",
        configuration={
            **configuration,
            "storage.service_directory": directory,
            "storage.model_cache": model_cache,
            "native_owner": ownership,
            "compatibility_profile": PROFILE,
            "port": str(port),
        },
        api_key=api_key,
        key_variable="LLAMA_API_KEY",
        runtime_files={
            "runtime.json": json.dumps(
                {
                    "flags": flags,
                    "served_model": served,
                    "backend": backend,
                    "environment": dict(compiled.env) if compiled else {},
                    "model": configuration.get("model", "").strip(),
                    "model_cache": model_cache,
                    "context_length": (compiled.values.get("context_length") if compiled else None),
                }
            ),
            "runtime-models.json": json.dumps([served]),
        },
        windows=windows,
    )
    if engine == "ollama" and action == "start" and configuration.get("model") and plan.readiness:
        return replace(
            plan,
            after_ready_hook=ollama_context_hook(
                port,
                configuration["model"],
                plan.readiness.logs,
                windows,
                sizing_request("ollama", configuration),
                cpu_threads=backend == "cpu",
            ),
        )
    return plan
