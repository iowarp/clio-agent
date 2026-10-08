"""Managed model servers -- vLLM, llama.cpp and Ollama -- in any negotiated container runtime.

One driver serves the three engines. Each is declared by an :class:`EngineSpec`
(pinned images per variant, port, health path, container name) plus its typed
server parameters (:mod:`clio_agent.gact.infrastructure.server_parameters`).
A deployment:

1. negotiates the container runtime with the target
   (:func:`~clio_agent.gact.infrastructure.container_runtime.negotiate_runtime`);
2. removes CLIO's own leftover container of the same name (never anything
   else) and refuses a port another process holds, with a typed reason;
3. creates the service directory, pulls the pinned image and starts the
   server bound to the target's loopback -- recording each thing it created in
   the service's ledger (:mod:`clio_agent.gact.infrastructure.resource_ledger`);
4. waits for the server to answer (no fixed deadline: a server that exits is
   reported with its logs, the person's cancel stops a slow load), then pulls
   the Ollama model when the engine is Ollama.

Model servers listen on the target's loopback only and are reached through the
SSH forward (or loopback locally): on a shared cluster login or compute node a
``0.0.0.0`` listener would serve any user on the cluster network. The loopback
is still shared by every user of that node, so vLLM and llama.cpp also get a
per-deployment API key (:mod:`clio_agent.gact.infrastructure.server_access`),
handed over through the environment (:mod:`~clio_agent.gact.infrastructure.secret_env`).
"""

from __future__ import annotations

import ntpath
import posixpath
from dataclasses import dataclass, field

from clio_agent.gact.infrastructure.container_runtime import (
    ContainerLaunch,
    RuntimeUnavailableError,
    exec_command,
    image_present_command,
    logs_command,
    negotiate_runtime,
    parse_runtime_name,
    pull_commands,
    remove_container_command,
    run_command,
    start_command,
    status_command,
    stop_command,
    usable_runtimes,
)
from clio_agent.gact.infrastructure.llama_native_windows import (
    LLAMA_BUILD,
    LLAMA_WINDOWS_CPU_ARCHIVE,
    NATIVE_PORT,
    native_windows_llama_plan,
)
from clio_agent.gact.infrastructure.model_runtime_readiness import (
    health_command,
    identity_health_command,
)
from clio_agent.gact.infrastructure.models import (
    RUNTIME_LABELS,
    CommandSpec,
    ContainerRuntimeFact,
    InfrastructureTarget,
    ManagedServiceDefinition,
    OwnedResource,
    RuntimeName,
    ServiceConfigurationField,
    ServiceVariant,
    TargetFacts,
)
from clio_agent.gact.infrastructure.native_vllm import (
    NATIVE_VARIANTS,
    native_variants,
    native_vllm_plan,
    served_model,
)
from clio_agent.gact.infrastructure.plan import DriverPlan, Readiness
from clio_agent.gact.infrastructure.resource_ledger import (
    StepRecorder,
    container_recorder,
    create_directory_command,
    directory_recorder,
    image_recorder,
    removal_commands,
    remove_container,
    shared_image_recorder,
)
from clio_agent.gact.infrastructure.secret_env import with_secret_env
from clio_agent.gact.infrastructure.server_access import KEY_VARIABLES, supports_api_key
from clio_agent.gact.infrastructure.server_parameter_defaults import parser_defaults
from clio_agent.gact.infrastructure.server_parameters import (
    EngineId,
    compile_parameters,
    engine_parameters,
)

VLLM_VERSION = "0.28.0"
OLLAMA_VERSION = "0.34.4"
# Images are pinned by registry digest, not by tag: a tag can be re-pushed. The
# tag each digest was resolved from is kept beside it for readers; container
# runtimes reject a reference that carries both a tag and a digest.
VLLM_IMAGES = {
    # vllm/vllm-openai*:v0.28.0, resolved 2026-10-08.
    "cuda": "vllm/vllm-openai@sha256:61fc8a896b0a4fbbbdc063bc4b0dbc25ce98e02b5050c24aeb7830ac02039b14",
    "rocm": "vllm/vllm-openai-rocm@sha256:e0a3b2bd3fe7ec563916c3a5d949898d133458c18d6b2f460c906885cfb32032",
    "cpu": "vllm/vllm-openai-cpu@sha256:197354e6475e638eb5ccca09beb078488046a82de0e204e7429bf1a804df9647",
}
# ghcr.io/ggml-org/llama.cpp:server[-vulkan]-b11206, resolved 2026-10-08.
LLAMA_CPU_IMAGE = "ghcr.io/ggml-org/llama.cpp@sha256:00bd6c289c590e576948cb3639b8195e4d3e6dd6ba1d761f0e2b871fcd4df24f"
LLAMA_VULKAN_IMAGE = "ghcr.io/ggml-org/llama.cpp@sha256:00810eb17c7b816f4e4b16ac9467ed177f125ff3d72abc61e4a8b717f4430bdc"
# ghcr.io/ggml-org/llama.cpp:server-cuda-b11206, resolved 2026-10-08.
LLAMA_CUDA_IMAGE = "ghcr.io/ggml-org/llama.cpp@sha256:3e7673cce183a55f97a1bc3c80817f3c61452483c13bc088a6766388af4775fe"
#: Offload every layer on a GPU variant unless the person sets gpu_layers.
LLAMA_ALL_LAYERS = "999"
# ollama/ollama:0.34.4 and :0.34.4-rocm, resolved 2026-10-08.
OLLAMA_IMAGE = (
    "ollama/ollama@sha256:8262851b2846b87c649eddf3e76beb270c52f4d1bc94559f47efde16b0841551"
)
OLLAMA_ROCM_IMAGE = (
    "ollama/ollama@sha256:f1f51e73691bd77e38a30b61f745f8d074fc664967ba8e209989091bd73b5adc"
)
MODEL_RUNTIME_SERVICES = frozenset({"vllm", "llama_cpp", "ollama"})
RUNTIME_FIELD = "container_runtime"
PORT_FIELD = "port"


@dataclass(frozen=True)
class VariantSpec:
    """One installable variant of an engine."""

    id: str
    label: str
    version: str
    image: str
    accelerator: str = "none"
    linux_only: bool = True
    x86_only: bool = False


@dataclass(frozen=True)
class EngineSpec:
    """Static facts about one managed engine."""

    engine: EngineId
    label: str
    description: str
    port: int
    container_name: str
    health_path: str
    variants: tuple[VariantSpec, ...]
    fields: tuple[ServiceConfigurationField, ...] = field(default_factory=tuple)


def _field(
    fid: str, label: str, placeholder: str, *, required: bool = False
) -> ServiceConfigurationField:
    return ServiceConfigurationField(
        id=fid, label=label, placeholder=placeholder, required=required
    )


ENGINES: dict[str, EngineSpec] = {
    "vllm": EngineSpec(
        engine="vllm",
        label="vLLM",
        description="OpenAI-compatible model serving.",
        port=8000,
        container_name="clio-vllm",
        health_path="/health",
        variants=(
            VariantSpec("cuda", "NVIDIA CUDA", VLLM_VERSION, VLLM_IMAGES["cuda"], "nvidia"),
            VariantSpec("rocm", "AMD ROCm", VLLM_VERSION, VLLM_IMAGES["rocm"], "amd"),
            VariantSpec("cpu", "CPU", VLLM_VERSION, VLLM_IMAGES["cpu"], x86_only=True),
        ),
        fields=(_field("model", "Model", "Qwen/Qwen3-8B", required=True),),
    ),
    "llama_cpp": EngineSpec(
        engine="llama_cpp",
        label="llama.cpp",
        description="Lightweight GGUF model serving.",
        port=8088,
        container_name="clio-llama-cpp",
        health_path="/health",
        variants=(
            VariantSpec("cuda", "NVIDIA CUDA", LLAMA_BUILD, LLAMA_CUDA_IMAGE, "nvidia"),
            VariantSpec("vulkan", "Vulkan", LLAMA_BUILD, LLAMA_VULKAN_IMAGE, "dri"),
            VariantSpec("cpu", "CPU", LLAMA_BUILD, LLAMA_CPU_IMAGE, linux_only=False),
        ),
        fields=(
            _field("model_path", "GGUF model path", "/models/model.gguf"),
            _field("hf_model", "Hugging Face GGUF model", "Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M"),
        ),
    ),
    "ollama": EngineSpec(
        engine="ollama",
        label="Ollama",
        description="Ollama model serving with model pull.",
        port=11434,
        container_name="clio-ollama",
        health_path="/api/version",
        variants=(
            VariantSpec("cpu", "CPU", OLLAMA_VERSION, OLLAMA_IMAGE),
            VariantSpec("cuda", "NVIDIA CUDA", OLLAMA_VERSION, OLLAMA_IMAGE, "nvidia"),
            VariantSpec("rocm", "AMD ROCm", OLLAMA_VERSION, OLLAMA_ROCM_IMAGE, "amd"),
        ),
        fields=(_field("model", "Model", "qwen2.5:0.5b", required=True),),
    ),
}


def service_port(service_id: str, configuration: dict[str, str], variant_id: str = "") -> int:
    """The target loopback port a model runtime listens on (configurable for containers)."""

    if variant_id == "native-windows-cpu":
        return NATIVE_PORT
    raw = configuration.get(PORT_FIELD, "").strip()
    if not raw:
        return ENGINES[service_id].port
    if not raw.isdigit() or not 1024 <= int(raw) <= 65535:
        raise ValueError("port must be a number from 1024 to 65535")
    return int(raw)


#: GPUs with a Vulkan driver CLIO can hand to a container: NVIDIA ships one in
#: its driver (exposed with --nv / NVIDIA_DRIVER_CAPABILITIES=graphics), AMD
#: through /dev/dri (Mesa RADV).
VULKAN_ACCELERATORS = frozenset({"nvidia", "amd"})


def _variant_compatible(variant: VariantSpec, facts: TargetFacts) -> tuple[bool, str]:
    if variant.linux_only and facts.os != "linux":
        return False, f"{variant.label} requires a Linux target."
    if variant.x86_only and facts.arch != "x86_64":
        return False, f"{variant.label} requires an x86-64 target."
    if variant.accelerator == "dri" and facts.accelerator not in VULKAN_ACCELERATORS:
        return False, f"{variant.label} requires an NVIDIA or AMD GPU."
    wanted = {"nvidia": "nvidia", "amd": "amd"}.get(variant.accelerator)
    if wanted and facts.accelerator != wanted:
        return (
            False,
            f"{variant.label} requires {'an NVIDIA' if wanted == 'nvidia' else 'an AMD'} GPU.",
        )
    try:
        runtime = negotiate_runtime(_runtimes(facts))
    except RuntimeUnavailableError as exc:
        return False, str(exc)
    return True, f"Runs with {RUNTIME_LABELS[runtime.name]}."


def _runtimes(facts: TargetFacts) -> list[ContainerRuntimeFact]:
    """Probed runtimes; a probe without runtime lines still reports Docker."""

    if facts.container_runtimes:
        return list(facts.container_runtimes)
    return [
        ContainerRuntimeFact(
            name="docker",
            installed=facts.docker_installed,
            usable=facts.docker_available,
            reason=None
            if facts.docker_available
            else ("unusable" if facts.docker_installed else "not_installed"),
            detail="" if facts.docker_available else "the Docker engine is not reachable",
        )
    ]


def model_runtime_definition(service_id: str, facts: TargetFacts) -> ManagedServiceDefinition:
    """The catalog row for one model runtime on the inspected target."""

    spec = ENGINES[service_id]
    variants: list[ServiceVariant] = []
    if service_id == "vllm":
        variants.extend(native_variants(facts))
    native = (
        service_id == "llama_cpp"
        and facts.target_id == "local"
        and facts.os == "windows"
        and facts.arch == "x86_64"
    )
    if service_id == "llama_cpp":
        variants.append(
            ServiceVariant(
                id="native-windows-cpu",
                label="Windows CPU (native)",
                version=LLAMA_BUILD,
                install_type="native_archive",
                artifact=LLAMA_WINDOWS_CPU_ARCHIVE,
                compatible=native,
                reason="Requires the CLIO host to be 64-bit Windows.",
            )
        )
    for variant in spec.variants:
        compatible, reason = _variant_compatible(variant, facts)
        variants.append(
            ServiceVariant(
                id=variant.id,
                label=variant.label,
                version=variant.version,
                install_type="container",
                artifact=variant.image,
                compatible=compatible,
                reason=reason,
            )
        )
    recommended = next((row.id for row in variants if row.compatible), spec.variants[-1].id)
    if native:
        recommended = "native-windows-cpu"
    runtime_options = usable_runtimes(_runtimes(facts))
    fields = [
        *spec.fields,
        ServiceConfigurationField(
            id=RUNTIME_FIELD,
            label="Container runtime",
            placeholder=(
                f"Automatic ({RUNTIME_LABELS[runtime_options[0]]})"
                if runtime_options
                else "No usable runtime"
            ),
            options=list(runtime_options),
            variants=[item.id for item in spec.variants],
        ),
        _field(PORT_FIELD, "Port", str(spec.port)),
    ]
    if service_id == "vllm":
        fields.append(
            ServiceConfigurationField(
                id="flowcept_settings",
                label="Flowcept settings on this host",
                placeholder="/data/provenance/settings.yaml",
                required=True,
                variants=["native-cuda-attention"],
            )
        )
    return ManagedServiceDefinition(
        id=service_id,
        category="model_runtime",
        label=spec.label,
        description=spec.description,
        recommended_variant=recommended,
        variants=variants,
        configuration_fields=fields,
        parameters=engine_parameters(spec.engine),
        supports_api_key=supports_api_key(service_id),
    )


def _service_dir(spec: EngineSpec, facts: TargetFacts, target: InfrastructureTarget | None) -> str:
    windows = facts.os == "windows"
    module = ntpath if windows else posixpath
    if target and any(target.storage.model_dump().values()):
        from clio_agent.gact.infrastructure.storage import resolved_locations  # noqa: PLC0415

        locations = resolved_locations(target, facts)
        return module.join(
            locations.service_data, facts.hostname or facts.target_id, spec.container_name
        )
    root = (target.install_root.strip() if target else "").rstrip("/\\")
    if not root:
        root = facts.agent_data_root
    if not root:
        if not facts.home:
            raise ValueError(
                "Could not determine the target's home directory; set an install location for this host."
            )
        root = (
            module.join(facts.home, "AppData", "Local", "clio-agent", "data")
            if windows
            else module.join(facts.home, "Library", "Application Support", "clio-agent", "data")
            if facts.os == "macos"
            else module.join(facts.home, ".local", "share", "clio-agent")
        )
    if not module.isabs(root):
        raise ValueError("The target's Agent data directory must be absolute")
    # Cluster nodes share one home: without the host in the path, a login node
    # and a compute node deploying the same engine would share one cache and
    # one SIF, and uninstalling on one would delete the other's.
    host = facts.hostname or facts.target_id
    return module.join(root, "services", host, spec.container_name)


def deployment_storage_configuration(
    service_id: str,
    facts: TargetFacts,
    target: InfrastructureTarget,
    configuration: dict[str, str],
    owned: list[OwnedResource],
) -> dict[str, str]:
    """Freeze an existing model deployment's paths before changing host defaults.

    Legacy receipts recorded created directories but no resolved configuration.
    Prefer their actual cache directory, then resolve with the old host settings.
    """
    module = ntpath if facts.os == "windows" else posixpath
    spec = ENGINES[service_id]
    caches = [
        row.ref
        for row in owned
        if row.kind == "directory"
        and module.basename(row.ref) == "cache"
        and module.basename(module.dirname(row.ref)) == spec.container_name
    ]
    if len(caches) > 1:
        raise ValueError(
            f"Multiple model caches recorded for {service_id}; inspect its storage first"
        )
    service_dir = configuration.get("storage.service_directory") or (
        module.dirname(caches[0]) if caches else _service_dir(spec, facts, target)
    )
    return {
        **configuration,
        "storage.service_directory": service_dir,
        "storage.model_cache": configuration.get("storage.model_cache")
        or (caches[0] if caches else module.join(service_dir, "cache")),
        "storage.temporary": configuration.get("storage.temporary") or service_dir,
    }


def _port_free_command(port: int) -> CommandSpec:
    return CommandSpec(
        program="sh",
        args=[
            "-c",
            "if ! command -v ss >/dev/null 2>&1; then "
            'echo "port_check_unavailable: ss is not installed; port $0 was not checked"; exit 0; fi; '
            'if [ -n "$(ss -Hltn "sport = :$0" 2>/dev/null)" ]; then '
            'echo "port_in_use: port $0 on this host is already in use by another process; '
            'choose another port for this service."; exit 3; fi; true',
            str(port),
        ],
    )


def _launch(
    spec: EngineSpec,
    variant: VariantSpec,
    runtime: RuntimeName,
    configuration: dict[str, str],
    port: int,
    cache_dir: str,
    windows: bool,
    keyed: bool = False,
    host_accelerator: str = "none",
) -> ContainerLaunch:
    module = ntpath if windows else posixpath
    if spec.engine == "vllm" and module.isabs(configuration.get("model", "")):
        # Same family-based tool/reasoning parser defaults as native vLLM
        # (user overrides kept), read from the downloaded model's config.
        configuration = {
            **configuration,
            **parser_defaults(configuration["model"], configuration),
        }
    compiled = compile_parameters(spec.engine, variant.id, configuration)
    if keyed and spec.engine not in KEY_VARIABLES:
        raise ValueError(f"{spec.label} has no API key support")
    host = "127.0.0.1" if runtime == "apptainer" else "0.0.0.0"
    env: list[tuple[str, str]] = [("HOME", "/cache")]
    if runtime == "docker" and not windows:
        env.extend([("USER", "clio"), ("LOGNAME", "clio")])
    mounts: list[tuple[str, str]] = []
    if spec.engine == "vllm":
        model = _required(configuration, "model")
        served: list[str] = []
        if module.isabs(model):
            _check_value("model_path", model)
            mounts.append((model, "/models/downloaded"))
            if "--served-model-name" not in compiled.flags:
                # Serve under the host path, as native vLLM does, so the id
                # CLIO discovers and binds is the model the user selected.
                served = ["--served-model-name", model]
            model = "/models/downloaded"
        env.append(("HF_HOME", "/cache/huggingface"))
        args = ["--model", model, *served, "--host", host, "--port", str(port), *compiled.flags]
    elif spec.engine == "llama_cpp":
        model_path = configuration.get("model_path", "").strip()
        hf_model = configuration.get("hf_model", "").strip()
        if bool(model_path) == bool(hf_model):
            raise ValueError(
                "Provide either a GGUF model path on the target or a Hugging Face GGUF model"
            )
        env.append(("LLAMA_CACHE", "/cache/llama.cpp"))
        if runtime == "apptainer":
            # The images find /app/*.so through their WORKDIR, which Apptainer
            # does not apply (`instance run` has no --pwd); keep the image's
            # own CUDA path, which an explicit value would replace (F033).
            env.append(("LD_LIBRARY_PATH", "/app:/usr/local/cuda/lib64"))
        if model_path:
            _check_value("model_path", model_path)
            mounts.append((model_path, "/models/model.gguf"))
            source = ["-m", "/models/model.gguf"]
            if "--alias" not in compiled.flags:
                # Serve under the host path, not the mount point, so the id
                # CLIO binds names the selected model and its family (F035).
                source.extend(["--alias", model_path])
        else:
            _check_value("hf_model", hf_model)
            source = ["-hf", hf_model]
        args = [*source, "--host", host, "--port", str(port), *compiled.flags]
        if variant.accelerator != "none" and "--n-gpu-layers" not in compiled.flags:
            args.extend(["--n-gpu-layers", LLAMA_ALL_LAYERS])
    else:
        env.extend([("OLLAMA_HOST", f"{host}:{port}"), ("OLLAMA_MODELS", "/cache/models")])
        args = ["serve"]
    env.extend(compiled.env)
    accelerator = variant.accelerator
    if accelerator == "dri" and host_accelerator == "nvidia":
        # Vulkan on NVIDIA: the driver's ICD comes with the GPU passthrough
        # (--gpus all / --nv), plus the graphics capability on Docker/Podman.
        accelerator = "nvidia"
        env.append(("NVIDIA_DRIVER_CAPABILITIES", "compute,utility,graphics"))
    return ContainerLaunch(
        name=spec.container_name,
        image=variant.image,
        port=port,
        container_port=port,
        args=tuple(args),
        env=tuple(env),
        cache_dir=cache_dir,
        accelerator=accelerator,
        mounts=tuple(mounts),
        # Docker/Podman take the key by name; Apptainer reads APPTAINERENV_*.
        secret_env=(KEY_VARIABLES[spec.engine],) if keyed and runtime != "apptainer" else (),
    )


def _llama_served_id(args: tuple[str, ...]) -> str:
    """The model id llama-server lists: its ``--alias``, else the ``-hf`` repo spec."""

    for flag in ("--alias", "-hf"):
        if flag in args and args.index(flag) + 1 < len(args):
            return args[args.index(flag) + 1]
    return ""


def _check_value(key: str, value: str) -> None:
    if any(character in value for character in ("\0", "\r", "\n")) or value.startswith("-"):
        raise ValueError(f"{key} is not a valid value")
    # A bind mount is `source:destination[:ro]` (and Apptainer splits --bind on
    # commas): such characters would change what is mounted.
    if key == "model_path" and any(character in value for character in (":", ",")):
        raise ValueError("model_path cannot contain ':' or ',' (it is bind-mounted)")


def _required(configuration: dict[str, str], key: str) -> str:
    value = configuration.get(key, "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    _check_value(key, value)
    return value


def build_model_runtime_plan(
    *,
    service_id: str,
    action: str,
    variant_id: str,
    configuration: dict[str, str],
    facts: TargetFacts,
    target: InfrastructureTarget | None,
    owned: list[OwnedResource] | None = None,
    api_key: str | None = None,
) -> DriverPlan:
    """Compile one lifecycle action for a managed model runtime.

    Args:
        service_id: ``vllm``, ``llama_cpp`` or ``ollama``.
        action: The lifecycle action.
        variant_id: The variant to install or the one installed.
        configuration: The request configuration (install) or the installed one.
        facts: The inspected target.
        target: The target record (install location, kind).
        owned: The service's ledger (for uninstall and reinstall).
        api_key: The deployment's API key (vLLM, llama.cpp), or ``None`` to run
            with no key. It reaches the server through the launch command's
            environment, read from stdin -- never an argument.

    Raises:
        ValueError: For an invalid configuration or parameter, or (as
            :class:`RuntimeUnavailableError`) when no usable runtime fits.
    """

    spec = ENGINES[service_id]
    if service_id == "vllm" and variant_id in NATIVE_VARIANTS:
        directory = configuration.get("storage.service_directory") or _service_dir(
            spec, facts, target
        )
        return native_vllm_plan(
            action,
            variant_id,
            configuration,
            facts,
            directory,
            service_port(service_id, configuration),
            api_key,
        )
    if api_key and service_id not in KEY_VARIABLES:
        raise ValueError(f"{spec.label} has no API key support")
    if service_id == "llama_cpp" and variant_id == "native-windows-cpu":
        compiled = compile_parameters("llama_cpp", "cpu", configuration)
        return native_windows_llama_plan(
            action, configuration.get("model_path", "").strip(), target, compiled.flags, api_key
        )
    variant = next((row for row in spec.variants if row.id == variant_id), None)
    if variant is None:
        raise ValueError(f"Unknown {service_id} variant {variant_id!r}")
    if action == "uninstall":
        # Straight from the ledger: each row names its own runtime, so an
        # uninstall still works after that runtime stopped being usable.
        if owned:
            return DriverPlan(tuple(removal_commands(owned, facts.os)), configuration=configuration)
        installed_runtime = parse_runtime_name(configuration.get(RUNTIME_FIELD, "") or "docker")
        return DriverPlan(
            (remove_container(installed_runtime, spec.container_name, facts.os),),
            configuration=configuration,
        )
    runtime_fact = negotiate_runtime(_runtimes(facts), configuration.get(RUNTIME_FIELD, ""))
    runtime = runtime_fact.name
    resolved = {**configuration, RUNTIME_FIELD: runtime}
    port = service_port(service_id, configuration)
    windows = facts.os == "windows"
    name = spec.container_name
    if action == "status":
        return DriverPlan(
            (status_command(runtime, name),), connection_port=port, configuration=resolved
        )
    if action == "logs":
        return DriverPlan(
            (logs_command(runtime, name),), connection_port=port, configuration=resolved
        )
    if action == "stop":
        return DriverPlan(
            (stop_command(runtime, name),), connection_port=port, configuration=resolved
        )
    service_dir = configuration.get("storage.service_directory") or _service_dir(
        spec, facts, target
    )
    module = ntpath if windows else posixpath
    cache_dir = configuration.get("storage.model_cache") or module.join(service_dir, "cache")
    temporary_dir = configuration.get("storage.temporary") or module.join(service_dir, "tmp")
    # Pulled images outlive one deployment's failure (they are a cache of a
    # pinned digest, not a half-built deployment) and are shared across hosts.
    image_store = module.join(module.dirname(service_dir.rstrip("/\\")), "apptainer-images")
    if target and any(target.storage.model_dump().values()):
        from clio_agent.gact.infrastructure.storage import resolved_locations  # noqa: PLC0415

        locations = resolved_locations(target, facts)
        cache_dir = configuration.get("storage.model_cache") or module.join(
            locations.models, facts.hostname or facts.target_id, spec.container_name
        )
        temporary_dir = configuration.get("storage.temporary") or module.join(
            locations.temporary, facts.hostname or facts.target_id, spec.container_name
        )
        image_store = module.join(locations.service_data, "apptainer-images")
    image_store = configuration.get("storage.image_store") or image_store
    resolved.update(
        {
            "storage.service_directory": service_dir,
            "storage.model_cache": cache_dir,
            "storage.temporary": temporary_dir,
        }
    )
    images_dir = module.join(service_dir, "images")
    launch = _launch(
        spec,
        variant,
        runtime,
        resolved,
        port,
        cache_dir,
        windows,
        keyed=bool(api_key),
        host_accelerator=facts.accelerator,
    )

    def keyed(command: CommandSpec) -> CommandSpec:
        if not api_key:
            return command
        variable = KEY_VARIABLES[spec.engine]
        if runtime == "apptainer":
            variable = f"APPTAINERENV_{variable}"
        return with_secret_env(command, variable, api_key, windows=windows)

    health_url = f"http://127.0.0.1:{port}{spec.health_path}"
    readiness = Readiness(
        # Ollama has no API key upstream: its identity is the owned container
        # alive plus /api/version answering.
        health=(
            identity_health_command(
                health_url,
                f"http://127.0.0.1:{port}/v1/models",
                served_model(launch.args[1], launch.args)
                if spec.engine == "vllm"
                else _llama_served_id(launch.args),
                KEY_VARIABLES[spec.engine],
                api_key,
                windows,
            )
            if api_key
            else health_command(health_url, windows)
        ),
        alive=status_command(runtime, name),
        logs=logs_command(runtime, name, lines=40),
        label=spec.label,
    )
    after_ready: tuple[CommandSpec, ...] = ()
    if spec.engine == "ollama":
        model = _required(resolved, "model")
        after_ready = (
            exec_command(
                runtime, name, ["env", f"OLLAMA_HOST=127.0.0.1:{port}", "ollama", "pull", model]
            ),
        )
    if action == "start":
        command = (
            start_command(runtime, name)
            if runtime != "apptainer"
            else keyed(run_command(runtime, launch, facts.identity, images_dir))
        )
        recorders: dict[int, StepRecorder] = (
            {0: container_recorder(runtime, name, facts.hostname)} if runtime == "apptainer" else {}
        )
        return DriverPlan(
            (command,),
            connection_port=port,
            recorders=recorders,
            readiness=readiness,
            configuration=resolved,
        )
    if action not in {"install", "reinstall"}:
        raise ValueError(f"Unsupported lifecycle action {action!r}")
    compatible, reason = _variant_compatible(variant, facts)
    if not compatible:
        raise ValueError(reason)
    commands: list[CommandSpec] = []
    recorders = {}
    if action == "reinstall":
        # Replace the server, keep the pulled image and downloaded models: they
        # stay on the ledger (and are removed by uninstall).
        running = [row for row in owned or [] if row.kind in {"container", "instance_logs"}]
        commands.extend(removal_commands(running, facts.os))
    # CLIO's own leftover of this service (same container name) is stopped,
    # never run beside; nothing else is touched.
    commands.append(remove_container_command(runtime, name))
    if not windows:
        commands.append(_port_free_command(port))
    directories = [cache_dir, temporary_dir] + ([images_dir] if runtime == "apptainer" else [])
    for directory in directories:
        recorders[len(commands)] = directory_recorder(directory, facts.os)
        commands.append(create_directory_command(directory, facts.os))
    recorders[len(commands)] = image_recorder(runtime, variant.image)
    commands.append(image_present_command(runtime, variant.image, images_dir, name))
    commands.extend(
        pull_commands(
            runtime,
            variant.image,
            images_dir,
            image_store,
            name,
            module.join(temporary_dir, "apptainer-tmp"),
        )
    )
    if runtime == "apptainer":
        recorders[len(commands) - 1] = shared_image_recorder()
    recorders[len(commands)] = container_recorder(runtime, name, facts.hostname)
    commands.append(
        keyed(
            run_command(runtime, launch, facts.identity, images_dir, rootless=runtime_fact.rootless)
        )
    )
    return DriverPlan(
        tuple(commands),
        connection_port=port,
        recorders=recorders,
        readiness=readiness,
        after_ready=after_ready,
        configuration=resolved,
    )
