"""Container runtime negotiation and per-runtime command construction.

A managed model server runs in whichever container runtime the target
actually has: Docker, Podman or Apptainer. Nothing here assumes one. The
target probe (:mod:`clio_agent.gact.infrastructure.probe`) reports what each
runtime looks like on the host -- installed, usable, and when unusable, a typed
reason with the runtime's own words -- and :func:`negotiate_runtime` picks one:
the person's explicit choice when it is usable, else the first usable runtime
in :data:`RUNTIME_PREFERENCE`. A runtime that cannot be used is never silently
swapped for another: an explicit choice that is unusable raises
:class:`RuntimeUnavailableError` carrying the typed reason, and so does a host
with no usable runtime at all.

The runtimes differ in ways the launch must respect:

* **Docker** runs containers as root by default, so files the server writes into
  a bind-mounted directory would be root-owned on the host and CLIO could not
  remove them on uninstall. CLIO passes ``--user <uid>:<gid>`` (both probed).
* **Podman** (rootless) already maps container root onto the calling user, so no
  ``--user`` is passed (it would map onto a sub-uid the user cannot delete).
* **Apptainer** runs as the calling user and shares the host network: there is
  no port publishing, so the server itself listens on the loopback port, and
  the image is converted to a SIF file inside a CLIO-owned directory with the
  layer cache pointed there as well (never the user's ``~/.apptainer`` cache).
"""

from __future__ import annotations

from dataclasses import dataclass

from clio_agent.gact.infrastructure.models import (
    RUNTIME_LABELS,
    CommandSpec,
    ContainerRuntimeFact,
    RuntimeName,
    TargetIdentity,
)

#: Separates the launched args from the environment in ``inspect`` output. Not
#: a tab: the Desktop's SSH PTY expands tabs to spaces.
INSPECT_SEPARATOR = " |clio| "

#: Negotiation order when the person does not choose: Docker first (the
#: runtime the existing deployments already use), then rootless Podman, then
#: Apptainer (the usual HPC runtime).
RUNTIME_PREFERENCE: tuple[RuntimeName, ...] = ("docker", "podman", "apptainer")


class RuntimeUnavailableError(ValueError):
    """No usable container runtime satisfies the request.

    Attributes:
        reason: ``runtime_not_usable`` (the chosen runtime cannot run
            containers), ``runtime_unknown`` (not a runtime CLIO drives), or
            ``no_usable_runtime`` (the host has none).
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def parse_runtime_name(value: str) -> RuntimeName:
    """A stored or requested runtime name as the typed :data:`RuntimeName`.

    Raises:
        RuntimeUnavailableError: ``runtime_unknown`` for anything CLIO does not drive.
    """

    name = value.strip().casefold()
    if name == "docker":
        return "docker"
    if name == "podman":
        return "podman"
    if name == "apptainer":
        return "apptainer"
    raise RuntimeUnavailableError(
        "runtime_unknown",
        f"{value!r} is not a container runtime CLIO can use "
        f"(choose {', '.join(RUNTIME_PREFERENCE)}).",
    )


def negotiate_runtime(
    runtimes: list[ContainerRuntimeFact], requested: str = ""
) -> ContainerRuntimeFact:
    """Pick the container runtime for a deployment.

    Args:
        runtimes: The probed runtime facts of the target.
        requested: An explicit runtime name, or ``""`` for automatic choice.

    Returns:
        The usable runtime fact to deploy with.

    Raises:
        RuntimeUnavailableError: When the requested runtime is unusable or
            unknown, or when no runtime on the target is usable.
    """

    by_name = {fact.name: fact for fact in runtimes}
    if requested.strip():
        wanted = parse_runtime_name(requested)
        fact = by_name.get(wanted)
        if fact is None or not fact.usable:
            explanation = (
                fact.explanation()
                if fact
                else f"{RUNTIME_LABELS[wanted]} was not inspected on this target."
            )
            raise RuntimeUnavailableError("runtime_not_usable", explanation)
        return fact
    for name in RUNTIME_PREFERENCE:
        fact = by_name.get(name)
        if fact is not None and fact.usable:
            return fact
    details = " ".join(
        (by_name.get(name) or ContainerRuntimeFact(name=name, reason="not_probed")).explanation()
        for name in RUNTIME_PREFERENCE
    )
    raise RuntimeUnavailableError(
        "no_usable_runtime", f"No container runtime can run services here. {details}"
    )


def usable_runtimes(runtimes: list[ContainerRuntimeFact]) -> list[RuntimeName]:
    """Usable runtime names in negotiation order."""

    usable = {fact.name for fact in runtimes if fact.usable}
    return [name for name in RUNTIME_PREFERENCE if name in usable]


@dataclass(frozen=True)
class ContainerLaunch:
    """Everything one runtime needs to start a managed server.

    Attributes:
        name: Container / instance name (CLIO-owned, stable per service).
        image: Pinned OCI image reference.
        port: Loopback port the server is reached on (host side).
        container_port: Port the server listens on inside the container.
        args: Arguments appended to the image entrypoint.
        env: Environment for the server.
        cache_dir: CLIO-owned host directory mounted at ``/cache`` (or ``""``).
        accelerator: ``"none"``, ``"nvidia"``, ``"amd"`` (ROCm) or ``"dri"`` (Vulkan).
        bind: Host address the published port binds to (Docker/Podman only).
        mounts: Extra read-only ``(host path, container path)`` binds.
        secret_env: Names of variables whose values the launch command's own
            environment carries (see :mod:`~clio_agent.gact.infrastructure.secret_env`):
            Docker/Podman take them by name, never with a value in the arguments.
    """

    name: str
    image: str
    port: int
    container_port: int
    args: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    cache_dir: str = ""
    accelerator: str = "none"
    bind: str = "127.0.0.1"
    mounts: tuple[tuple[str, str], ...] = ()
    secret_env: tuple[str, ...] = ()


def sif_path(images_dir: str, name: str) -> str:
    """Where Apptainer keeps the converted image for one service."""

    return f"{images_dir.rstrip('/')}/{name}.sif"


def image_present_command(
    runtime: RuntimeName, image: str, images_dir: str, name: str
) -> CommandSpec:
    """A command that exits 0 when the image is already on the target."""

    if runtime == "apptainer":
        return CommandSpec(
            program="test", args=["-f", sif_path(images_dir, name)], allowed_exit_codes=[0, 1]
        )
    return CommandSpec(
        program=runtime, args=["image", "inspect", image], allowed_exit_codes=[0, 1, 125]
    )


# One Apptainer pull attempt, bounded inside the script so a timeout stops
# apptainer itself (not only the shell). The SIF of a pinned image is kept in
# a shared store keyed by its digest, with a ``.ref`` sidecar written only
# after a complete pull; a store hit is reused instead of re-pulled, and the
# service's own SIF path is a symlink to it.
_APPTAINER_PULL_SCRIPT = """set -eu
store=$1; image=$2; key=$3; link=$4; scratch=$5; budget=$6
sif="$store/$key.sif"
mkdir -p "$store/cache" "$scratch"
if [ -f "$sif" ] && [ "$(cat "$sif.ref" 2>/dev/null)" = "$image" ]; then
  echo "clio: reusing $sif"
else
  APPTAINER_CACHEDIR="$store/cache" APPTAINER_TMPDIR="$scratch" \\
    timeout -k 15 "$budget" apptainer pull --force "$sif.partial" "docker://$image"
  mv -f "$sif.partial" "$sif"
  printf '%s\\n' "$image" > "$sif.ref"
fi
ln -sfn "$sif" "$link"
"""

# Two attempts of at most this many seconds each: the first may run out of
# time (exit 124) with the downloaded layers kept in the persistent layer
# cache; the second resumes from them, or is a no-op when the first finished.
APPTAINER_PULL_ATTEMPT_SECONDS = 1740


def image_store_key(image: str) -> str:
    """A file-name-safe key for a pinned image (its digest when it has one)."""

    if "@sha256:" in image:
        return "sha256-" + image.rsplit("@sha256:", 1)[1]
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in image)


def pull_commands(
    runtime: RuntimeName,
    image: str,
    images_dir: str,
    store_dir: str,
    name: str,
    scratch_dir: str | None = None,
) -> list[CommandSpec]:
    """Fetch the pinned image.

    Apptainer converts it to a SIF in ``store_dir`` (a persistent image store
    with its own layer cache, so a failed or timed-out pull resumes and a new
    node does not re-download) using ``scratch_dir`` (the target's temporary
    location, which an operator may point at node-local scratch) for the
    conversion. The pull runs as two bounded attempts so a large image is not
    cut off by one command's timeout.
    """

    if runtime != "apptainer":
        return [CommandSpec(program=runtime, args=["pull", image], timeout_seconds=1800)]
    args = [
        "-c",
        _APPTAINER_PULL_SCRIPT,
        "clio-apptainer-pull",
        store_dir.rstrip("/"),
        image,
        image_store_key(image),
        sif_path(images_dir, name),
        scratch_dir or f"{store_dir.rstrip('/')}/tmp",
        str(APPTAINER_PULL_ATTEMPT_SECONDS),
    ]
    first = CommandSpec(program="sh", args=args, timeout_seconds=1800, allowed_exit_codes=[0, 124])
    return [first, first.model_copy(update={"allowed_exit_codes": [0]})]


def remove_container_command(runtime: RuntimeName, name: str) -> CommandSpec:
    """Stop and remove this service's container / instance if it exists."""

    if runtime == "apptainer":
        return CommandSpec(
            program="apptainer", args=["instance", "stop", name], allowed_exit_codes=[0, 1, 255]
        )
    return CommandSpec(
        program=runtime, args=["rm", "--force", name], allowed_exit_codes=[0, 1], settle_seconds=1.0
    )


def run_command(
    runtime: RuntimeName,
    launch: ContainerLaunch,
    identity: TargetIdentity,
    images_dir: str,
    *,
    rootless: bool = False,
) -> CommandSpec:
    """Start the server detached in the negotiated runtime."""

    if runtime == "apptainer":
        args = ["instance", "run", "--cleanenv", "--writable-tmpfs"]
        # Apptainer refuses HOME through --env; --home mounts the CLIO-owned
        # directory at /cache AND makes it HOME, exactly what HOME=/cache means
        # for Docker and Podman.
        for key, value in launch.env:
            if key != "HOME":
                args.extend(["--env", f"{key}={value}"])
        if launch.cache_dir:
            args.extend(["--home", f"{launch.cache_dir}:/cache"])
        for source, destination in launch.mounts:
            args.extend(["--bind", f"{source}:{destination}:ro"])
        if launch.accelerator == "nvidia":
            args.append("--nv")
        elif launch.accelerator == "amd":
            args.append("--rocm")
        args.extend([sif_path(images_dir, launch.name), launch.name, *launch.args])
        return CommandSpec(program="apptainer", args=args, timeout_seconds=300)
    args = ["run", "--detach", "--name", launch.name]
    if runtime == "docker":
        args.extend(["--restart", "unless-stopped"])
        # Rootful Docker writes as root into the bind mount unless told who to
        # be; rootless Docker already maps root onto the user (``--user`` would
        # map onto a sub-uid the user cannot delete).
        if identity.known and not rootless:
            args.extend(["--user", f"{identity.uid}:{identity.gid}"])
    args.extend(["--publish", f"{launch.bind}:{launch.port}:{launch.container_port}"])
    if launch.cache_dir:
        args.extend(["--volume", f"{launch.cache_dir}:/cache"])
    for source, destination in launch.mounts:
        args.extend(["--volume", f"{source}:{destination}:ro"])
    for key, value in launch.env:
        args.extend(["--env", f"{key}={value}"])
    for key in launch.secret_env:
        # By name only: the value comes from this command's environment.
        args.extend(["--env", key])
    if launch.accelerator == "nvidia":
        args.extend(["--gpus", "all"])
    elif launch.accelerator == "amd":
        args.extend(["--device", "/dev/kfd", "--device", "/dev/dri", "--group-add", "video"])
    elif launch.accelerator == "dri":
        args.extend(["--device", "/dev/dri"])
    args.extend(["--ipc", "host", launch.image, *launch.args])
    return CommandSpec(program=runtime, args=args, timeout_seconds=300)


def start_command(runtime: RuntimeName, name: str) -> CommandSpec:
    """Restart a stopped Docker/Podman container (Apptainer re-runs the instance)."""

    return CommandSpec(program=runtime, args=["start", name])


def stop_command(runtime: RuntimeName, name: str) -> CommandSpec:
    """Stop the server, keeping its container (Docker/Podman) for a later start."""

    if runtime == "apptainer":
        return remove_container_command(runtime, name)
    return CommandSpec(program=runtime, args=["stop", name], timeout_seconds=120)


def status_command(runtime: RuntimeName, name: str) -> CommandSpec:
    """Print ``running``, ``exited``/``created`` or ``stopped`` for the service."""

    if runtime == "apptainer":
        return CommandSpec(
            program="sh",
            args=[
                "-c",
                'if apptainer instance list "$0" 2>/dev/null | tail -n +2 | grep -q .; '
                "then echo running; else echo stopped; fi",
                name,
            ],
        )
    return CommandSpec(
        program=runtime,
        args=["inspect", "--format", "{{.State.Status}}", name],
        allowed_exit_codes=[0, 1, 125],
    )


def logs_command(runtime: RuntimeName, name: str, lines: int = 80) -> CommandSpec:
    """Recent server output."""

    if runtime == "apptainer":
        return CommandSpec(
            program="sh",
            args=[
                "-c",
                'for f in "$HOME"/.apptainer/instances/logs/*/"$USER"/"$0".out '
                '"$HOME"/.apptainer/instances/logs/*/"$USER"/"$0".err; do '
                '[ -f "$f" ] && tail -n "$1" "$f"; done; true',
                name,
                str(lines),
            ],
        )
    return CommandSpec(program=runtime, args=["logs", "--tail", str(lines), name])


def log_line_command(runtime: RuntimeName, name: str, marker: str) -> CommandSpec:
    """The first line of the server's whole log containing ``marker`` (a startup line).

    Startup facts (vLLM's resolved engine config) are logged once, before any
    request, so a ``--tail`` window loses them on a busy server.
    """

    if runtime == "apptainer":
        return CommandSpec(
            program="sh",
            args=[
                "-c",
                'cat "$HOME"/.apptainer/instances/logs/*/"$USER"/"$0".out '
                '"$HOME"/.apptainer/instances/logs/*/"$USER"/"$0".err 2>/dev/null '
                '| grep -m1 -F -- "$1"; true',
                name,
                marker,
            ],
        )
    return CommandSpec(
        program="sh",
        args=["-c", '"$0" logs "$1" 2>&1 | grep -m1 -F -- "$2"; true', runtime, name, marker],
    )


def exec_command(
    runtime: RuntimeName, name: str, argv: list[str], timeout: float = 1800
) -> CommandSpec:
    """Run a command inside the running server (for example ``ollama pull``)."""

    if runtime == "apptainer":
        return CommandSpec(
            program="apptainer",
            args=["exec", f"instance://{name}", *argv],
            timeout_seconds=timeout,
        )
    return CommandSpec(program=runtime, args=["exec", name, *argv], timeout_seconds=timeout)


def inspect_config_command(runtime: RuntimeName, name: str) -> CommandSpec | None:
    """The launched arguments and environment, as the runtime reports them."""

    if runtime == "apptainer":
        return None
    return CommandSpec(
        program=runtime,
        args=[
            "inspect",
            "--format",
            f"{{{{json .Args}}}}{INSPECT_SEPARATOR}{{{{json .Config.Env}}}}",
            name,
        ],
        allowed_exit_codes=[0, 1, 125],
    )
