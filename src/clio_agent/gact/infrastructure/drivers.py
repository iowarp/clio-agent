"""Allowlisted service definitions and lifecycle command plans owned by CLIO."""

from __future__ import annotations

import importlib.metadata
import ntpath
import posixpath
from collections.abc import Callable
from dataclasses import dataclass

from clio_agent.gact.infrastructure.clio_agent_deploy import (
    LAUNCHER_PRELUDE,
    ClaimResult,
    claim_command,
    install_command,
    status_command,
    teardown_command,
)
from clio_agent.gact.infrastructure.models import (
    CommandSpec,
    InfrastructureTarget,
    ManagedServiceDefinition,
    ServiceConfigurationField,
    ServiceVariant,
    TargetFacts,
)

WEB_SEARCH_IMAGE = "ghcr.io/iowarp/clio-web-search:0.3.1"
VLLM_VERSION = "0.28.0"
LLAMA_BUILD = "b10621"
LLAMA_CPU_IMAGE = f"ghcr.io/ggml-org/llama.cpp:server-{LLAMA_BUILD}"
LLAMA_VULKAN_IMAGE = f"ghcr.io/ggml-org/llama.cpp:server-vulkan-{LLAMA_BUILD}"
LLAMA_WINDOWS_CPU_ARCHIVE = (
    "https://github.com/ggml-org/llama.cpp/releases/download/"
    f"{LLAMA_BUILD}/llama-{LLAMA_BUILD}-bin-win-cpu-x64.zip"
)
RELAY_VERSION = "1.6.8"
CLIO_AGENT_PORT = 17_800
# Services whose server listens on the target's loopback only: nothing can
# reach them at the host's address, so they are always reached through an SSH
# forward. CLIO's launcher binds 127.0.0.1.
LOOPBACK_ONLY_SERVICES = frozenset({"clio_agent"})


def clio_agent_version() -> str:
    """The clio-agent version this CLIO runs: the one a remote CLIO installs."""

    return importlib.metadata.version("clio-agent")


@dataclass(frozen=True)
class DriverPlan:
    """Commands and endpoint metadata for one validated lifecycle action."""

    commands: tuple[CommandSpec, ...]
    connection_port: int | None = None
    # Undo what this plan started when it fails or is cancelled, given what
    # its claim step found (see clio_agent_deploy); None when nothing to undo.
    teardown: Callable[[ClaimResult], CommandSpec] | None = None


def service_connection_port(service_id: str) -> int | None:
    """Return the stable listener port for a connectable managed service."""

    return {
        "vllm": 8000,
        "llama_cpp": 8088,
        "web_search": 8089,
        "clio_agent": CLIO_AGENT_PORT,
    }.get(service_id)


def _field(
    field_id: str,
    label: str,
    placeholder: str,
    *,
    required: bool = False,
    options: list[str] | None = None,
) -> ServiceConfigurationField:
    return ServiceConfigurationField(
        id=field_id,
        label=label,
        placeholder=placeholder,
        required=required,
        options=options or [],
    )


def service_definitions(facts: TargetFacts) -> list[ManagedServiceDefinition]:
    """Build service compatibility from inspected host facts."""

    docker = facts.docker_available
    linux = facts.os == "linux"
    vllm = ManagedServiceDefinition(
        id="vllm",
        category="model_runtime",
        label="vLLM",
        description="OpenAI-compatible model serving.",
        recommended_variant="cuda",
        variants=[
            ServiceVariant(
                id="cuda",
                label="NVIDIA CUDA",
                version=VLLM_VERSION,
                install_type="container",
                artifact=f"vllm/vllm-openai:v{VLLM_VERSION}",
                compatible=docker and linux and facts.accelerator == "nvidia",
                reason="Requires Linux, Docker, and an NVIDIA GPU.",
            ),
            ServiceVariant(
                id="rocm",
                label="AMD ROCm",
                version=VLLM_VERSION,
                install_type="container",
                artifact=f"vllm/vllm-openai-rocm:v{VLLM_VERSION}",
                compatible=docker and linux and facts.accelerator == "amd",
                reason="Requires Linux, Docker, and an AMD ROCm GPU.",
            ),
            ServiceVariant(
                id="cpu",
                label="CPU",
                version=VLLM_VERSION,
                install_type="container",
                artifact=f"vllm/vllm-openai-cpu:v{VLLM_VERSION}",
                compatible=docker and linux and facts.arch == "x86_64",
                reason="Requires x86-64 Linux and Docker; CPU serving may be slow.",
            ),
        ],
        configuration_fields=[_field("model", "Model", "Qwen/Qwen3-8B", required=True)],
    )
    llama = ManagedServiceDefinition(
        id="llama_cpp",
        category="model_runtime",
        label="llama.cpp",
        description="Lightweight GGUF model serving.",
        recommended_variant=(
            "native-windows-cpu"
            if facts.target_id == "local" and facts.os == "windows" and facts.arch == "x86_64"
            else "cpu"
        ),
        variants=[
            ServiceVariant(
                id="native-windows-cpu",
                label="Windows CPU (native)",
                version=LLAMA_BUILD,
                install_type="native_archive",
                artifact=LLAMA_WINDOWS_CPU_ARCHIVE,
                compatible=(
                    facts.target_id == "local" and facts.os == "windows" and facts.arch == "x86_64"
                ),
                reason="Requires the CLIO host to be 64-bit Windows.",
            ),
            ServiceVariant(
                id="vulkan",
                label="Vulkan",
                version=LLAMA_BUILD,
                install_type="container",
                artifact=LLAMA_VULKAN_IMAGE,
                compatible=docker and linux and facts.accelerator == "amd",
                reason="Requires Linux, Docker, and a Vulkan-capable AMD GPU.",
            ),
            ServiceVariant(
                id="cpu",
                label="CPU",
                version=LLAMA_BUILD,
                install_type="container",
                artifact=LLAMA_CPU_IMAGE,
                compatible=docker,
                reason="Requires Docker.",
            ),
        ],
        configuration_fields=[
            _field("model_path", "GGUF model path", "/models/model.gguf", required=True)
        ],
    )
    web_search = ManagedServiceDefinition(
        id="web_search",
        category="scientific_service",
        label="CLIO Web Search",
        description="Private search and document conversion.",
        recommended_variant="container",
        variants=[
            ServiceVariant(
                id="container",
                label="Docker",
                version="0.3.1",
                install_type="container",
                artifact=WEB_SEARCH_IMAGE,
                compatible=docker,
                reason=(
                    "Docker is ready."
                    if docker
                    else "Docker is installed but unavailable."
                    if facts.docker_installed
                    else "Docker is not installed."
                ),
            )
        ],
        configuration_fields=[
            _field("contact_email", "Publication metadata email", "scientist@example.org"),
            _field("task_backend_port", "Document task port", "8090"),
        ],
    )
    relay = ManagedServiceDefinition(
        id="relay",
        category="remote_access",
        label="CLIO Relay",
        description="Deploy and inspect persistent work on an SSH cluster.",
        recommended_variant="uv-tool",
        variants=[
            ServiceVariant(
                id="uv-tool",
                label="Released uv tool",
                version=RELAY_VERSION,
                install_type="uv_tool",
                artifact=f"clio-relay=={RELAY_VERSION}",
                compatible=facts.uv_available,
                reason="Requires uv on the managing CLIO host.",
            )
        ],
        configuration_fields=[
            _field("cluster_name", "Cluster name", "my-cluster", required=True),
            _field("agent_bin", "Remote agent executable", "agent", required=True),
            _field(
                "relay_artifact_sha256", "Relay wheel SHA-256", "Published SHA-256", required=True
            ),
        ],
        supports_stop=False,
    )
    clio_agent = ManagedServiceDefinition(
        id="clio_agent",
        category="remote_access",
        label="CLIO",
        description="Install and operate a CLIO service on this SSH host.",
        recommended_variant="released",
        variants=[
            # Exactly this CLIO's version, so the desktop and remote match.
            ServiceVariant(
                id="released",
                label="Published release",
                version=clio_agent_version(),
                install_type="bootstrap",
                artifact=f"clio-agent=={clio_agent_version()} (PyPI)",
                compatible=facts.os == "linux" and facts.target_id != "local",
                reason="Remote CLIO deployment requires a Linux SSH target.",
            )
        ],
    )
    return [vllm, llama, web_search, relay, clio_agent]


def _required(configuration: dict[str, str], key: str) -> str:
    value = configuration.get(key, "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    if "\0" in value or "\r" in value or "\n" in value:
        raise ValueError(f"{key} contains invalid control characters")
    return value


def _docker_lifecycle(action: str, container: str) -> DriverPlan:
    if action == "status":
        return DriverPlan(
            (
                CommandSpec(
                    program="docker", args=["inspect", "--format", "{{.State.Status}}", container]
                ),
            )
        )
    if action == "logs":
        return DriverPlan(
            (CommandSpec(program="docker", args=["logs", "--tail", "80", container]),)
        )
    if action == "stop":
        return DriverPlan((CommandSpec(program="docker", args=["stop", container]),))
    if action == "uninstall":
        return DriverPlan(
            (
                CommandSpec(
                    program="docker",
                    args=["rm", "--force", container],
                    allowed_exit_codes=[0, 1],
                    settle_seconds=1.0,
                ),
            )
        )
    raise ValueError(f"Unsupported lifecycle action {action!r}")


def build_driver_plan(
    *,
    service_id: str,
    action: str,
    variant_id: str,
    configuration: dict[str, str],
    facts: TargetFacts,
    target: InfrastructureTarget | None = None,
) -> DriverPlan:
    """Compile one allowlisted lifecycle action into commands."""

    definitions = {row.id: row for row in service_definitions(facts)}
    definition = definitions.get(service_id)
    if definition is None:
        raise ValueError(f"Unknown managed service {service_id!r}")
    variant = next((row for row in definition.variants if row.id == variant_id), None)
    if variant is None:
        raise ValueError(f"Unknown {service_id} variant {variant_id!r}")
    if action in {"install", "reinstall"} and not variant.compatible:
        raise ValueError(variant.reason or "This service is unavailable on the selected target")

    if service_id == "relay":
        return _relay_plan(action, configuration, target)
    if service_id == "clio_agent":
        return _clio_agent_plan(action, target)

    if service_id == "llama_cpp" and variant_id == "native-windows-cpu":
        return _native_windows_llama_plan(action, configuration, target)

    container = {
        "vllm": "clio-vllm",
        "llama_cpp": "clio-llama-cpp",
        "web_search": "clio-web-search",
    }[service_id]
    if action in {"status", "logs", "stop", "uninstall"}:
        return _docker_lifecycle(action, container)
    if action == "start":
        return DriverPlan(
            (CommandSpec(program="docker", args=["start", container]),),
            connection_port={"vllm": 8000, "llama_cpp": 8088, "web_search": 8089}[service_id],
        )

    commands: list[CommandSpec] = []
    if action == "reinstall":
        commands.append(
            CommandSpec(
                program="docker",
                args=["rm", "--force", container],
                allowed_exit_codes=[0, 1],
                settle_seconds=1.0,
            )
        )
    commands.append(
        CommandSpec(program="docker", args=["pull", variant.artifact], timeout_seconds=900)
    )
    storage = _container_storage_path(service_id, target, facts)
    if storage:
        commands.append(_create_directory(storage, facts))
    commands.append(
        _container_run(
            service_id,
            variant.artifact,
            variant_id,
            configuration,
            facts,
            storage,
        )
    )
    return DriverPlan(
        tuple(commands),
        connection_port={"vllm": 8000, "llama_cpp": 8088, "web_search": 8089}[service_id],
    )


def _container_run(
    service_id: str,
    artifact: str,
    variant_id: str,
    configuration: dict[str, str],
    facts: TargetFacts,
    storage: str | None,
) -> CommandSpec:
    bind = "127.0.0.1" if facts.target_id == "local" else "0.0.0.0"
    if service_id == "web_search":
        email = configuration.get("contact_email", "").strip()
        task_port = configuration.get("task_backend_port", "8090").strip() or "8090"
        if not task_port.isdigit() or not 1 <= int(task_port) <= 65535:
            raise ValueError("task_backend_port must be a valid port")
        args = [
            "run",
            "--detach",
            "--name",
            "clio-web-search",
            "--restart",
            "unless-stopped",
            "--publish",
            f"{bind}:8089:8080",
            "--publish",
            f"{bind}:{task_port}:6379",
            "--volume",
            f"{storage or 'clio-web-search-data'}:/var/lib/clio-web-search",
            "--env",
            f"CLIO_WEB_SEARCH_TASK_BACKEND_PUBLIC_PORT={task_port}",
        ]
        if email:
            if "@" not in email or any(character.isspace() for character in email):
                raise ValueError("contact_email must be a valid email address")
            args.extend(["--env", f"CLIO_WEB_SEARCH_CONTACT_EMAIL={email}"])
        args.append(artifact)
        return CommandSpec(program="docker", args=args)
    if service_id == "llama_cpp":
        model_path = _required(configuration, "model_path")
        args = [
            "run",
            "--detach",
            "--name",
            "clio-llama-cpp",
            "--restart",
            "unless-stopped",
            "--publish",
            f"{bind}:8088:8080",
            "--volume",
            f"{model_path}:/models/model.gguf:ro",
        ]
        if variant_id == "vulkan":
            args.extend(["--device", "/dev/dri"])
        args.extend([artifact, "-m", "/models/model.gguf", "--host", "0.0.0.0", "--port", "8080"])
        return CommandSpec(program="docker", args=args)
    model = _required(configuration, "model")
    args = [
        "run",
        "--detach",
        "--name",
        "clio-vllm",
        "--restart",
        "unless-stopped",
        "--publish",
        f"{bind}:8000:8000",
    ]
    if storage:
        args.extend(["--volume", f"{storage}:/root/.cache/huggingface"])
    if variant_id == "cuda":
        args.extend(["--gpus", "all"])
    elif variant_id == "rocm":
        args.extend(["--device", "/dev/kfd", "--device", "/dev/dri", "--group-add", "video"])
    args.extend(["--ipc", "host", artifact, "--model", model])
    return CommandSpec(program="docker", args=args)


def _container_storage_path(
    service_id: str,
    target: InfrastructureTarget | None,
    facts: TargetFacts,
) -> str | None:
    """Return an optional service-owned bind directory under the configured root."""

    if target is None or not target.install_root.strip() or service_id == "llama_cpp":
        return None
    root = target.install_root.strip().rstrip("/\\")
    name = {"web_search": "web-search", "vllm": "vllm-cache"}.get(service_id)
    if name is None:
        return None
    path_module = ntpath if facts.os == "windows" else posixpath
    return path_module.join(root, "services", name)


def _create_directory(path: str, facts: TargetFacts) -> CommandSpec:
    """Create one validated driver-owned directory without invoking a shell on Linux."""

    if facts.os == "windows":
        return CommandSpec(
            program="powershell",
            args=[
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "New-Item -ItemType Directory -Force -LiteralPath $args[0] | Out-Null",
                path,
            ],
        )
    return CommandSpec(program="mkdir", args=["-p", "--", path])


def _relay_plan(
    action: str,
    configuration: dict[str, str],
    target: InfrastructureTarget | None,
) -> DriverPlan:
    cluster = _required(configuration, "cluster_name")
    if action in {"status", "logs"}:
        return DriverPlan(
            (
                CommandSpec(
                    program="clio-relay",
                    args=["cluster", "endpoint-service-status", "--cluster", cluster],
                    scope="controller",
                ),
            )
        )
    if action == "stop":
        raise ValueError("Relay workers are persistent; use Relay teardown for destructive cleanup")
    if action == "uninstall":
        return DriverPlan(
            (
                CommandSpec(
                    program="uv",
                    args=["tool", "uninstall", "clio-relay"],
                    scope="controller",
                    allowed_exit_codes=[0, 1],
                ),
            )
        )
    agent = _required(configuration, "agent_bin")
    sha = _required(configuration, "relay_artifact_sha256")
    if len(sha) != 64 or any(character not in "0123456789abcdefABCDEF" for character in sha):
        raise ValueError("relay_artifact_sha256 must contain exactly 64 hexadecimal characters")
    commands = [
        CommandSpec(
            program="uv",
            args=[
                "tool",
                "install",
                "--python",
                "3.12",
                "--no-config",
                f"clio-relay=={RELAY_VERSION}",
            ],
            scope="controller",
            timeout_seconds=900,
        ),
        CommandSpec(
            program="clio-relay",
            args=[
                "cluster",
                "add",
                "--name",
                cluster,
                "--ssh-host",
                _ssh_destination(target),
                "--scheduler-provider",
                "slurm",
                "--agent-adapter",
                "exec",
                "--agent-bin",
                agent,
            ],
            scope="controller",
        ),
        CommandSpec(
            program="clio-relay",
            args=["cluster", "bootstrap", "--cluster", cluster, "--relay-artifact-sha256", sha],
            scope="controller",
            timeout_seconds=900,
        ),
        CommandSpec(
            program="clio-relay",
            args=[
                "cluster",
                "install-endpoint-service",
                "--cluster",
                cluster,
                "--start",
                "--enable",
            ],
            scope="controller",
            timeout_seconds=900,
        ),
    ]
    return DriverPlan(tuple(commands))


def _native_windows_llama_plan(
    action: str,
    configuration: dict[str, str],
    target: InfrastructureTarget | None,
) -> DriverPlan:
    if target is None or target.kind != "local":
        raise ValueError("Native Windows llama.cpp can run only beside the active CLIO")
    root = target.install_root.strip()
    prefix = (
        "$root=$args[0]; if (!$root) { "
        f"$root=Join-Path $env:LOCALAPPDATA 'CLIO\\services\\llama.cpp\\{LLAMA_BUILD}'"
        " };"
    )
    invoke = ["-NoProfile", "-NonInteractive", "-Command"]
    if action == "status":
        script = (
            prefix + "$pidFile=Join-Path $root 'server.pid'; "
            "if (!(Test-Path -LiteralPath $pidFile)) { Write-Output 'stopped'; exit 0 }; "
            "$process=Get-Process -Id (Get-Content -LiteralPath $pidFile) -ErrorAction SilentlyContinue; "
            "if ($process) { Write-Output 'running' } else { Write-Output 'stopped' }"
        )
        return DriverPlan((CommandSpec(program="powershell", args=[*invoke, script, root]),))
    if action == "logs":
        script = (
            prefix + "Get-Content -LiteralPath (Join-Path $root 'server.log') -Tail 80 "
            "-ErrorAction SilentlyContinue; Get-Content -LiteralPath "
            "(Join-Path $root 'server-error.log') -Tail 80 -ErrorAction SilentlyContinue"
        )
        return DriverPlan((CommandSpec(program="powershell", args=[*invoke, script, root]),))
    if action == "stop":
        script = (
            prefix
            + "$pidFile=Join-Path $root 'server.pid'; if (Test-Path -LiteralPath $pidFile) { "
            "Stop-Process -Id (Get-Content -LiteralPath $pidFile) -ErrorAction SilentlyContinue; "
            "Remove-Item -LiteralPath $pidFile -Force }"
        )
        return DriverPlan((CommandSpec(program="powershell", args=[*invoke, script, root]),))
    if action == "uninstall":
        stop = _native_windows_llama_plan("stop", configuration, target).commands[0]
        remove = CommandSpec(
            program="powershell",
            args=[
                *invoke,
                prefix + "if (Test-Path -LiteralPath $root) { Remove-Item -Recurse -Force $root }",
                root,
            ],
        )
        return DriverPlan((stop, remove))
    model_path = _required(configuration, "model_path")
    commands: list[CommandSpec] = []
    if action == "reinstall":
        commands.extend(_native_windows_llama_plan("uninstall", configuration, target).commands)
    if action in {"install", "reinstall"}:
        install = (
            "$ErrorActionPreference='Stop'; "
            + prefix
            + "New-Item -ItemType Directory -Force -Path $root | Out-Null; "
            "$archive=Join-Path $env:TEMP 'clio-llama.zip'; "
            "Invoke-WebRequest -UseBasicParsing -Uri $args[1] -OutFile $archive; "
            "Expand-Archive -LiteralPath $archive -DestinationPath $root -Force; "
            "Remove-Item -LiteralPath $archive -Force; "
            "$exe=Get-ChildItem -LiteralPath $root -Filter 'llama-server.exe' -Recurse | "
            "Select-Object -First 1; if (!$exe) { throw 'llama-server.exe was not installed' }; "
            "& $exe.FullName --version | Out-Null; if ($LASTEXITCODE -ne 0) { "
            "throw 'llama-server --version failed' }"
        )
        commands.append(
            CommandSpec(
                program="powershell",
                args=[*invoke, install, root, LLAMA_WINDOWS_CPU_ARCHIVE],
                timeout_seconds=900,
            )
        )
    start = (
        "$ErrorActionPreference='Stop'; "
        + prefix
        + "$exe=Get-ChildItem -LiteralPath $root -Filter 'llama-server.exe' -Recurse | "
        "Select-Object -First 1; if (!$exe) { throw 'Install llama.cpp before starting it' }; "
        "$stdout=Join-Path $root 'server.log'; $stderr=Join-Path $root 'server-error.log'; "
        "$process=Start-Process -FilePath $exe.FullName -ArgumentList "
        "@('-m',$args[1],'--host','127.0.0.1','--port','8088') "
        "-RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru; "
        "Set-Content -LiteralPath (Join-Path $root 'server.pid') -Value $process.Id"
    )
    commands.append(CommandSpec(program="powershell", args=[*invoke, start, root, model_path]))
    return DriverPlan(tuple(commands), connection_port=8088)


def _ssh_destination(target: InfrastructureTarget | None) -> str:
    if target is None or target.kind != "ssh" or target.ssh is None:
        raise ValueError("Relay installation requires an SSH infrastructure target")
    if target.ssh.profile.strip():
        return target.ssh.profile.strip()
    host = target.ssh.host.strip()
    if not host:
        raise ValueError("Relay target requires an SSH host")
    return f"{target.ssh.user.strip()}@{host}" if target.ssh.user.strip() else host


def _clio_agent_plan(action: str, target: InfrastructureTarget | None) -> DriverPlan:
    if target is None or target.kind != "ssh":
        raise ValueError("Remote CLIO deployment requires an SSH infrastructure target")
    root = target.install_root.strip()

    def launcher(script: str) -> CommandSpec:
        return CommandSpec(program="bash", args=["-lc", LAUNCHER_PRELUDE + script, "clio", root])

    if action == "status":
        return DriverPlan((status_command(root, CLIO_AGENT_PORT),), connection_port=CLIO_AGENT_PORT)
    if action == "logs":
        return DriverPlan((launcher('"$bin/clio" logs'),), connection_port=CLIO_AGENT_PORT)
    if action == "stop":
        return DriverPlan((launcher('"$bin/clio" stop'),))
    if action == "uninstall":
        return DriverPlan(
            (
                launcher(
                    'marker="$root/.clio-managed-install"; '
                    'if [ ! -f "$marker" ]; then '
                    "echo 'Refusing to remove an unmarked install root' >&2; exit 73; fi; "
                    '"$bin/clio" stop || true; rm -rf -- "$root"'
                ),
            )
        )
    commands: list[CommandSpec] = []
    if action == "reinstall":
        commands.extend(_clio_agent_plan("uninstall", target).commands)
    if action not in {"install", "reinstall", "start"}:
        raise ValueError(f"Unsupported CLIO lifecycle action {action!r}")
    version = clio_agent_version()
    # Adopt this install's healthy server of this version, or stop any other
    # CLIO on the port, before touching anything; never start beside one.
    commands.append(claim_command(root, CLIO_AGENT_PORT, version))
    if action in {"install", "reinstall"}:
        commands.append(install_command(root, version))
    commands.append(launcher('"$bin/clio" start'))
    return DriverPlan(
        tuple(commands),
        connection_port=CLIO_AGENT_PORT,
        teardown=lambda claim: teardown_command(
            root, CLIO_AGENT_PORT, purge_root=not claim.existing_root
        ),
    )
