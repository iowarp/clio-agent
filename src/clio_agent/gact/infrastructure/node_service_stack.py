"""Target-side container dependencies for a definition-owned monitoring service.

This stdlib helper is installed beside the shared native supervisor. Every
container and network has an exact owner label. Images are resolved once at
installation and their immutable IDs are retained in the deployment receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

OWNER_LABEL = "ai.iowarp.clio.deployment"
RUNTIME_PARENT = Path("/run/user")


def host_network(manifest: dict[str, Any]) -> bool:
    """Apptainer instances share the host network (no publish, no daemon)."""
    return manifest.get("container_runtime") == "apptainer"


def apptainer_backend() -> Any:
    """The shipped Apptainer backend beside this file."""
    import stack_apptainer  # type: ignore[import-not-found]

    return stack_apptainer


def flowcept_environment(root: Path) -> None:
    """Bind the managed collector and probes to their private deployment settings."""
    for name in tuple(os.environ):
        if name.startswith(("MQ_", "KVDB_", "MONGO_", "LMDB_")):
            os.environ.pop(name)
    os.environ["FLOWCEPT_SETTINGS_PATH"] = str(root / "settings.yaml")


def engine_capacity(root: Path, required: int) -> None:
    """Check the actual image store, which may differ from the selected data volume."""
    info = json.loads(command(root, ["info", "--format", "{{json .}}"]))
    directory = info.get("DockerRootDir") or info.get("store", {}).get("graphRoot")
    if not directory or not Path(directory).is_absolute():
        raise ValueError("Cannot determine the container engine's image storage directory")
    if shutil.disk_usage(directory).free < required:
        raise ValueError(
            f"Insufficient image-store space at {directory}: requires {required} free bytes. "
            "Choose an existing image or move the engine storage before deploying."
        )


def command(root: Path, arguments: list[str], *, env: dict[str, str] | None = None) -> str:
    """Run the selected engine without printing private container environment."""
    manifest = json.loads((root / "manifest.json").read_text())
    program, environment = engine_invocation(root, manifest, env)
    building = arguments[0] == "build"
    result = subprocess.run(
        [*program, *arguments],
        env=environment,
        # Build progress belongs in the supervisor's durable install log. The
        # pinned source recipe receives no generated database credentials.
        stdout=sys.stdout if building else subprocess.PIPE,
        stderr=subprocess.STDOUT if building else subprocess.PIPE,
        text=True,
        timeout=1800 if building else 900,
        check=False,
    )
    if result.returncode:
        diagnostics = ((result.stdout or "") + (result.stderr or ""))[-16000:]
        credentials = root / "credentials.json"
        if credentials.is_file():
            for secret in json.loads(credentials.read_text()).values():
                if secret:
                    diagnostics = diagnostics.replace(str(secret), "[redacted]")
        log = root / "logs/container-engine.log"
        if log.is_symlink():
            raise ValueError("Container diagnostic log cannot be a symlink")
        log.parent.mkdir(exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        log.write_text(f"{stamp} Container {arguments[0]} failed\n" + diagnostics[-15800:])
        raise RuntimeError(f"Container {arguments[0]} failed (exit {result.returncode})")
    return (result.stdout or "") + ((result.stderr or "") if arguments[0] == "logs" else "")


def engine_invocation(
    root: Path, manifest: dict[str, Any], env: dict[str, str] | None
) -> tuple[list[str], dict[str, str] | None]:
    """Keep an explicitly selected Podman store inside this deployment's owned folder."""
    program = [manifest["container_runtime"]]
    if manifest.get("image_storage", "engine") == "engine":
        return program, env
    if manifest.get("image_storage") != "service" or program != ["podman"]:
        raise ValueError("Only Podman supports deployment-owned image storage")
    paths = {name: root / "containers" / name for name in ("images", "downloads")}
    for path in paths.values():
        if path.resolve() != path or root not in path.parents:
            raise ValueError("Container storage cannot leave the owned service directory")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    runroot = podman_runroot(root)
    environment = dict(os.environ if env is None else env)
    # A remote engine or inherited storage options must not redirect these paths.
    for key in ("CONTAINER_HOST", "CONTAINER_CONNECTION", "CONTAINERS_STORAGE_CONF"):
        environment.pop(key, None)
    environment["TMPDIR"] = str(paths["downloads"])
    program += [
        "--remote=false",
        "--root",
        str(paths["images"]),
        "--runroot",
        str(runroot),
    ]
    return program, environment


def podman_runroot(root: Path) -> Path:
    """Use short, private runtime state on tmpfs; image bytes stay in the service folder."""
    uid = os.getuid()
    base = RUNTIME_PARENT / str(uid)
    if not base.is_dir() or base.resolve() != base or base.stat().st_uid != uid:
        raise ValueError("Podman needs the execution user's private /run/user directory")
    directory = base / ("clio-" + hashlib.sha256(str(root).encode()).hexdigest()[:16])
    if directory.is_symlink():
        raise ValueError("Podman runtime storage cannot be a symlink")
    directory.mkdir(mode=0o700, exist_ok=True)
    marker = directory / "clio-owner.json"
    expected = json.loads((root / "owner.json").read_text())
    if marker.is_symlink():
        raise ValueError("Podman runtime ownership cannot be a symlink")
    if marker.exists():
        if json.loads(marker.read_text()) != expected:
            raise ValueError("Podman runtime storage belongs to another deployment")
    elif any(directory.iterdir()):
        raise ValueError("Refusing to adopt non-empty Podman runtime storage")
    else:
        with marker.open("x") as stream:
            json.dump(expected, stream)
    return directory


def inspect(root: Path, kind: str, name: str) -> dict[str, Any] | None:
    """Distinguish a missing resource from an unavailable engine."""
    # Listing succeeds even when the named resource is absent, unlike inspect.
    arguments = (
        [kind, "ls", "--format", "{{.Name}}"]
        if kind in {"network", "pod"}
        else ["ps", "-a", "--format", "{{.Names}}"]
    )
    if name not in command(root, arguments).splitlines():
        return None
    payload = json.loads(command(root, [kind, "inspect", name]))
    row = payload[0] if isinstance(payload, list) else payload
    expected = json.loads((root / "owner.json").read_text())["owner"]
    labels = row.get("Labels") if kind in {"network", "pod"} else row["Config"].get("Labels")
    if kind == "network" and labels is None:
        # Podman CNI and Netavark use different inspect envelopes.
        labels = row.get("labels") or row.get("args", {}).get("podman_labels")
    if (labels or {}).get(OWNER_LABEL) != expected:
        raise ValueError("A monitoring resource name belongs to another deployment")
    return row


def private_configuration(root: Path, manifest: dict[str, Any]) -> dict[str, str]:
    """Generate credentials on the execution host and keep them outside receipts."""
    path = root / "credentials.json"
    if any(
        (root / name).is_symlink() for name in ("credentials.json", "settings.yaml", "redis.conf")
    ):
        raise ValueError("Monitoring credentials path is a symlink")
    if not path.exists():
        path.write_text(json.dumps({"password": secrets.token_urlsafe(32)}))
        path.chmod(0o600)
    password = json.loads(path.read_text())["password"]
    if manifest["service"] == "flowcept":
        settings = manifest["settings"]
        settings["mq"]["uri"] = f"redis://:{password}@127.0.0.1:{manifest['redis_port']}/0"
        settings["kv_db"]["uri"] = settings["mq"]["uri"]
        settings["databases"]["mongodb"]["uri"] = (
            f"mongodb://clio:{password}@127.0.0.1:{manifest['mongo_port']}/?authSource=admin"
        )
        # JSON is a valid YAML subset and can be read by Flowcept/OmegaConf.
        (root / "settings.yaml").write_text(json.dumps(settings))
        (root / "settings.yaml").chmod(0o600)
        # Behind a published port Redis listens on the container's interface;
        # on the host network it binds loopback at its private port.
        listen = (
            f"bind 127.0.0.1\nport {manifest['redis_port']}\n"
            if host_network(manifest)
            else "bind 0.0.0.0\n"
        )
        (root / "redis.conf").write_text(
            f"{listen}protected-mode yes\nrequirepass {password}\nappendonly yes\ndir /data\n"
        )
        (root / "redis.conf").chmod(0o600)
    return {
        "REDISCLI_AUTH": password,
        "MONGO_INITDB_ROOT_USERNAME": "clio",
        "MONGO_INITDB_ROOT_PASSWORD": password,
        "POSTGRES_USER": "clio",
        "POSTGRES_PASSWORD": password,
        "POSTGRES_DB": "clio",
    }


def install(root: Path, manifest: dict[str, Any]) -> None:
    """Resolve pinned definition images; leave the service and databases stopped."""
    private = private_configuration(root, manifest)
    if host_network(manifest):
        apptainer_backend().cleanup(root, manifest, private)
        apptainer_backend().install(root, manifest)
        return
    # Replacement keeps database/evidence files but cannot retain old container
    # specifications after ports, images or environment have changed.
    cleanup(root, manifest, remove=True)
    build = manifest.get("build")
    if build:
        engine_capacity(root, 8 * 1024**3)
        source = root / "source"
        if source.is_symlink():
            raise ValueError("Monitoring source directory cannot be a symlink")
        if not source.exists():
            subprocess.run(
                ["git", "clone", "--no-checkout", build["repository"], str(source)],
                check=True,
                capture_output=True,
                timeout=180,
            )
        subprocess.run(
            ["git", "-C", str(source), "checkout", "--detach", build["revision"]],
            check=True,
            capture_output=True,
            timeout=60,
        )
        dockerfile = source / build["dockerfile"]
        if build.get("base_image"):
            dockerfile = build_definition(root, dockerfile, build)
        command(
            root,
            [
                "build",
                *(["--layers=true"] if manifest["container_runtime"] == "podman" else []),
                "--label",
                "org.opencontainers.image.revision=" + build["revision"],
                "--file",
                str(dockerfile),
                "--tag",
                build["image"],
                str(source),
            ],
        )
    images: dict[str, str] = {}
    components = list(manifest["components"])
    if manifest.get("pod_infra_image"):
        components.append({"name": "pod_infra", "image": manifest["pod_infra_image"]})
    for component in components:
        image = component["image"]
        try:
            details = json.loads(command(root, ["image", "inspect", image]))[0]
        except RuntimeError:
            if image.startswith("sha256:"):
                raise ValueError(
                    "The chosen local image is unavailable on this execution host"
                ) from None
            engine_capacity(root, 2 * 1024**3)
            command(root, ["pull", image])
            details = json.loads(command(root, ["image", "inspect", image]))[0]
        images[component["name"]] = details["Id"]
    temporary = root / "images.json.tmp"
    if temporary.is_symlink():
        raise ValueError("Image receipt path cannot be a symlink")
    temporary.write_text(json.dumps(images))
    temporary.replace(root / "images.json")


def build_definition(root: Path, upstream: Path, build: dict[str, Any]) -> Path:
    """Keep the pinned upstream recipe while replacing its retired OS base explicitly."""
    content = upstream.read_text()
    expected = "FROM " + build["upstream_base"]
    if [line for line in content.splitlines() if line.startswith("FROM ")] != [expected]:
        raise ValueError(
            "The pinned service build recipe no longer matches its compatibility profile"
        )
    if "@sha256:" not in build["base_image"]:
        raise ValueError("The service build base must have an immutable digest")
    output = root / "Containerfile"
    if output.is_symlink():
        raise ValueError("Service build definition cannot be a symlink")
    output.write_text(content.replace(expected, "FROM " + build["base_image"], 1))
    return output


def start(root: Path, manifest: dict[str, Any]) -> None:
    """Start only owned dependency containers, with data in the chosen host root."""
    if host_network(manifest):
        if manifest["service"] == "cmf":
            for directory in ("static", "env", "labels", "tensorboard-logs"):
                (root / "data/cmf" / directory).mkdir(parents=True, exist_ok=True)
        apptainer_backend().start(root, manifest, private_configuration(root, manifest))
        return
    owner = json.loads((root / "owner.json").read_text())["owner"]
    network = manifest["network"]
    images = json.loads((root / "images.json").read_text())
    pod = manifest["container_runtime"] == "podman"
    rootless = False
    if pod:
        info = json.loads(command(root, ["info", "--format", "json"]))
        rootless = bool(info.get("host", {}).get("security", {}).get("rootless"))
        if inspect(root, "pod", network) is None:
            args = [
                "pod",
                "create",
                "--name",
                network,
                "--label",
                f"{OWNER_LABEL}={owner}",
                "--infra-image",
                images["pod_infra"],
                "--infra-command",
                "/pause",
            ]
            if rootless:
                args += ["--network", "slirp4netns"]
            for component in manifest["components"]:
                for host, container in component.get("ports", []):
                    args += ["--publish", f"127.0.0.1:{host}:{container}"]
            command(root, args)
    elif inspect(root, "network", network) is None:
        command(root, ["network", "create", "--label", f"{OWNER_LABEL}={owner}", network])
    env = {**os.environ, **private_configuration(root, manifest)}
    if manifest["service"] == "cmf":
        for directory in ("static", "env", "labels", "tensorboard-logs"):
            (root / "data/cmf" / directory).mkdir(parents=True, exist_ok=True)
    for component in manifest["components"]:
        name = component["name"]
        row = inspect(root, "container", name)
        if row is not None:
            if row["Image"] != images[name]:
                raise ValueError("Remove the previous runtime before changing its installed image")
            if not row["State"]["Running"]:
                command(root, ["start", name])
        else:
            arguments = [
                "run",
                "--detach",
                "--name",
                name,
                "--label",
                f"{OWNER_LABEL}={owner}",
                *(
                    ["--pod", network]
                    if pod
                    else ["--network", network, "--network-alias", component["role"]]
                ),
                "--restart",
                "no",
                "--user",
                "0:0" if rootless else f"{os.getuid()}:{os.getgid()}",
            ]
            if not pod:
                for host, container in component.get("ports", []):
                    arguments += ["--publish", f"127.0.0.1:{host}:{container}"]
            for relative, destination, readonly in component.get("mounts", []):
                path = root / relative
                if root not in path.parents or path.resolve() != path:
                    raise ValueError("Monitoring storage must stay within its owned host directory")
                if not readonly:
                    path.mkdir(parents=True, exist_ok=True)
                arguments += ["--volume", f"{path}:{destination}" + (":ro" if readonly else "")]
            for name in component.get("secrets", []):
                arguments += ["--env", name]
            for key, value in component.get("environment", {}).items():
                arguments += ["--env", f"{key}={value}"]
            if component.get("entrypoint"):
                arguments += ["--entrypoint", component["entrypoint"]]
            arguments += [images[component["name"]], *component.get("arguments", [])]
            command(root, arguments, env=env)
        # Database readiness is checked by its native client before consumers start.
        if component.get("check"):
            deadline = time.monotonic() + 60
            while True:
                try:
                    command(root, ["exec", component["name"], *component["check"]])
                    break
                except RuntimeError:
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"{component['role']} did not become ready") from None
                    time.sleep(1)
        if component.get("initialize"):
            command(root, ["exec", component["name"], *component["initialize"]])


def collect_logs(root: Path, manifest: dict[str, Any]) -> None:
    """Refresh bounded logs from existing owned containers without changing their state."""
    private = json.loads((root / "credentials.json").read_text())
    if host_network(manifest):
        apptainer_backend().collect_logs(root, manifest, private)
        return
    for component in manifest["components"]:
        name = component["name"]
        if inspect(root, "container", name) is None:
            continue
        logs = command(root, ["logs", "--tail", "100", name])[-16000:]
        for value in private.values():
            if value:
                logs = logs.replace(str(value), "[redacted]")
        (root / "logs" / (component["role"] + ".log")).write_text(logs)


def cleanup(root: Path, manifest: dict[str, Any], *, remove: bool) -> None:
    """Stop/remove only matching owned containers, retaining every data directory."""
    if host_network(manifest):
        private = json.loads((root / "credentials.json").read_text())
        apptainer_backend().cleanup(root, manifest, private)
        return
    collect_logs(root, manifest)
    for component in reversed(manifest["components"]):
        name = component["name"]
        row = inspect(root, "container", name)
        if row is None:
            continue
        if row["State"]["Running"]:
            command(root, ["stop", "--time", "15", name])
        observed = inspect(root, "container", name)
        if observed and observed["State"]["Running"]:
            raise RuntimeError("An owned monitoring container is still running")
        if remove:
            command(root, ["rm", name])
    if remove and inspect(root, "network", manifest["network"]) is not None:
        command(root, ["network", "rm", manifest["network"]])
    pod = (
        inspect(root, "pod", manifest["network"])
        if manifest["container_runtime"] == "podman"
        else None
    )
    if pod:
        expected = {row["name"] for row in manifest["components"]}
        if any(
            row.get("Id") != pod.get("InfraContainerID") and row.get("Name") not in expected
            for row in pod.get("Containers", [])
        ):
            raise ValueError("The owned pod contains an unexpected container; cleanup refused")
        command(root, ["pod", "stop", "--time", "15", manifest["network"]])
        if remove:
            command(root, ["pod", "rm", manifest["network"]])


def delete_images(root: Path, manifest: dict[str, Any]) -> None:
    """Delete only this deployment's retained image store, after all runtimes are removed."""
    if host_network(manifest):
        apptainer_backend().delete_images(root, manifest)
        return
    if manifest.get("image_storage") != "service":
        return
    if command(root, ["ps", "-a", "--quiet"]).strip():
        raise ValueError("The deployment image store still has containers; remove them first")
    if command(root, ["images", "--quiet"]).strip():
        # Let Podman remove files with subordinate-UID ownership. Never reset the
        # engine: network configuration may be shared outside its image root.
        command(root, ["rmi", "--all"])
    if command(root, ["images", "--quiet"]).strip():
        raise RuntimeError("The deployment image store still contains retained images")
    shutil.rmtree(podman_runroot(root))


def delete_podman_data(root: Path, manifest: dict[str, Any]) -> None:
    """Remove explicitly deleted owned database data through its rootless UID mapping."""
    if manifest["container_runtime"] != "podman":
        return
    if inspect(root, "pod", manifest["network"]) is not None:
        raise ValueError("Remove the owned pod before deleting retained data")
    data = root / "data"
    if data.resolve() != data:
        raise ValueError("Service data cannot leave its owned directory")
    if not data.exists():
        return
    info = json.loads(command(root, ["info", "--format", "json"]))
    if info.get("host", {}).get("security", {}).get("rootless"):
        command(
            root,
            [
                "unshare",
                "python3",
                "-c",
                "import shutil,sys; shutil.rmtree(sys.argv[1])",
                str(data),
            ],
        )
        if data.exists():
            raise RuntimeError("Owned service data remains after rootless deletion")


def running(root: Path, manifest: dict[str, Any], component: dict[str, Any]) -> bool:
    """Runtime-neutral liveness of one owned component."""
    if host_network(manifest):
        return bool(apptainer_backend().running(root, component))
    row = inspect(root, "container", component["name"])
    return bool(row and row["State"]["Running"])


def main() -> None:
    """Run a definition-owned installation or cleanup hook."""
    os.umask(0o077)
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "manifest.json").read_text())
    action = sys.argv[1]
    if action == "install":
        install(root, manifest)
    elif action in {"stop", "uninstall"}:
        cleanup(root, manifest, remove=action == "uninstall")
    elif action == "logs":
        collect_logs(root, manifest)
    elif action == "delete_data":
        if any(
            (
                apptainer_backend().running(root, row)
                if host_network(manifest)
                else inspect(root, "container", row["name"]) is not None
            )
            for row in manifest["components"]
        ):
            raise ValueError("Remove the monitoring runtime before deleting retained data")
        delete_podman_data(root, manifest)
        delete_images(root, manifest)
    else:
        raise ValueError("Unsupported monitoring hook")


if __name__ == "__main__":
    main()
