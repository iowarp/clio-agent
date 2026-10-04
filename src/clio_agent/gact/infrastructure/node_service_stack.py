"""Target-side container dependencies for a definition-owned monitoring service.

This stdlib helper is installed beside the shared native supervisor. Every
container and network has an exact owner label. Images are resolved once at
installation and their immutable IDs are retained in the deployment receipt.
"""

from __future__ import annotations

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
    result = subprocess.run(
        [manifest["container_runtime"], *arguments],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Container {arguments[0]} failed (exit {result.returncode})")
    return result.stdout + (result.stderr if arguments[0] == "logs" else "")


def inspect(root: Path, kind: str, name: str) -> dict[str, Any] | None:
    """Distinguish a missing resource from an unavailable engine."""
    # Listing succeeds even when the named resource is absent, unlike inspect.
    arguments = (
        [kind, "ls", "--format", "{{.Name}}"]
        if kind == "network"
        else ["ps", "-a", "--format", "{{.Names}}"]
    )
    if name not in command(root, arguments).splitlines():
        return None
    row = json.loads(command(root, [kind, "inspect", name]))[0]
    expected = json.loads((root / "owner.json").read_text())["owner"]
    labels = row.get("Labels") if kind == "network" else row["Config"].get("Labels")
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
        (root / "redis.conf").write_text(
            f"bind 0.0.0.0\nprotected-mode yes\nrequirepass {password}\nappendonly yes\ndir /data\n"
        )
        (root / "redis.conf").chmod(0o600)
    return {
        "MONGO_INITDB_ROOT_USERNAME": "clio",
        "MONGO_INITDB_ROOT_PASSWORD": password,
        "POSTGRES_USER": "clio",
        "POSTGRES_PASSWORD": password,
        "POSTGRES_DB": "clio",
    }


def install(root: Path, manifest: dict[str, Any]) -> None:
    """Resolve pinned definition images; leave the service and databases stopped."""
    private_configuration(root, manifest)
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
        command(
            root,
            [
                "build",
                "--label",
                "org.opencontainers.image.revision=" + build["revision"],
                "--file",
                str(source / build["dockerfile"]),
                "--tag",
                build["image"],
                str(source),
            ],
        )
    images: dict[str, str] = {}
    for component in manifest["components"]:
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


def start(root: Path, manifest: dict[str, Any]) -> None:
    """Start only owned dependency containers, with data in the chosen host root."""
    owner = json.loads((root / "owner.json").read_text())["owner"]
    network = manifest["network"]
    if inspect(root, "network", network) is None:
        command(root, ["network", "create", "--label", f"{OWNER_LABEL}={owner}", network])
    images = json.loads((root / "images.json").read_text())
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
                "--network",
                network,
                "--network-alias",
                component["role"],
                "--restart",
                "no",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
            ]
            if manifest["container_runtime"] == "podman":
                info = json.loads(command(root, ["info", "--format", "json"]))
                if info.get("host", {}).get("security", {}).get("rootless"):
                    arguments += ["--userns", "keep-id"]
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
            inspect(root, "container", row["name"]) is not None for row in manifest["components"]
        ):
            raise ValueError("Remove the monitoring runtime before deleting retained data")
    else:
        raise ValueError("Unsupported monitoring hook")


if __name__ == "__main__":
    main()
