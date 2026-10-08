"""Target-side Apptainer dependencies for a definition-owned monitoring service.

Shipped beside ``stack.py`` as ``stack_apptainer.py`` (stdlib only). Apptainer
has no daemon, networks or labels: every component is a user instance named
with the deployment prefix, started from a SIF converted into the owned
``containers/images`` folder. Ownership is that name prefix plus the SIF path
and its sha256 recorded at installation. Instances share the host network, so
each dependency listens on loopback at its private host port; secrets reach
the instance as ``APPTAINERENV_*`` in the process environment, never argv.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


def owned_path(root: Path, relative: str) -> Path:
    """Resolve a path that must stay inside the owned service directory."""
    path = root / relative
    if root not in path.parents or path.resolve() != path:
        raise ValueError("Apptainer storage must stay within its owned host directory")
    return path


def apptainer(
    root: Path, arguments: list[str], *, env: dict[str, str] | None = None, timeout: int = 900
) -> str:
    """Run Apptainer with its cache and scratch inside the deployment's folder."""
    downloads = owned_path(root, "containers/downloads")
    downloads.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = dict(os.environ if env is None else env)
    environment.update(APPTAINER_CACHEDIR=str(downloads), APPTAINER_TMPDIR=str(downloads))
    result = subprocess.run(
        ["apptainer", *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        diagnostics = (result.stdout or "") + (result.stderr or "")
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
        log.write_text(f"{stamp} Apptainer {arguments[0]} failed\n" + diagnostics[-15800:])
        raise RuntimeError(f"Apptainer {' '.join(arguments[:2])} failed (exit {result.returncode})")
    return result.stdout or ""


def sif(root: Path, component: dict[str, Any]) -> Path:
    """The owned SIF for one component."""
    return owned_path(root, f"containers/images/{component['role']}.sif")


def digest(path: Path) -> str:
    """sha256 of an installed SIF (its immutable identity in the receipt)."""
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            hasher.update(block)
    return "sha256:" + hasher.hexdigest()


def instances(root: Path) -> dict[str, dict[str, Any]]:
    """This user's instances by name (Apptainer lists only the caller's)."""
    payload = json.loads(apptainer(root, ["instance", "list", "--json"]) or "{}")
    return {row["instance"]: row for row in payload.get("instances") or []}


def owned_instance(root: Path, component: dict[str, Any]) -> dict[str, Any] | None:
    """The named instance, refusing one that runs another image."""
    row = instances(root).get(component["name"])
    if row is None:
        return None
    if Path(row.get("img", "")) != sif(root, component):
        raise ValueError("A monitoring instance name belongs to another deployment")
    return row


def install(root: Path, manifest: dict[str, Any]) -> None:
    """Convert each digest-pinned image to an owned SIF and record its sha256."""
    images: dict[str, str] = {}
    for component in manifest["components"]:
        image = component["image"]
        if "@sha256:" not in image:
            raise ValueError("Apptainer monitoring images must be digest-pinned")
        path = sif(root, component)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.is_file():
            apptainer(root, ["pull", "--force", str(path), f"docker://{image}"], timeout=1800)
        images[component["name"]] = digest(path)
    temporary = root / "images.json.tmp"
    if temporary.is_symlink():
        raise ValueError("Image receipt path cannot be a symlink")
    temporary.write_text(json.dumps(images))
    temporary.replace(root / "images.json")


def install_environment(root: Path, manifest: dict[str, Any]) -> None:
    """Install a component venv from the shipped hash-pinned lock inside its base SIF.

    The venv is reused while its marker records the same lock digest and revision.
    """
    spec = manifest["source_environment"]
    lock = owned_path(root, spec["lock"])
    marker = owned_path(root, "cmf-venv/.clio-installed")
    identity = digest(lock) + " " + spec["revision"]
    if marker.is_file() and marker.read_text() == identity:
        return
    venv = owned_path(root, "cmf-venv")
    if venv.exists():
        shutil.rmtree(venv)
    scratch = owned_path(root, "containers/downloads/pip-tmp")
    for path in (venv, scratch):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    server = next(row for row in manifest["components"] if row["role"] == "server")
    pip = "/cmf-server/venv/bin/pip"
    script = (
        "set -e; python -m venv /cmf-server/venv; "
        f"{pip} install --no-cache-dir --disable-pip-version-check --require-hashes --no-deps "
        "--only-binary :all: "
        + "".join(f"--no-binary {name} " for name in spec.get("sdists", []))
        + "-r /lock.txt; "
        f"{pip} install --no-cache-dir --disable-pip-version-check --no-deps "
        "--no-build-isolation --no-index /cmf-server/src"
    )
    apptainer(
        root,
        [
            "exec",
            "--cleanenv",
            "--containall",
            "--bind",
            f"{venv}:/cmf-server/venv",
            "--bind",
            f"{scratch}:/tmp",
            "--bind",
            f"{lock}:/lock.txt:ro",
            "--bind",
            f"{owned_path(root, 'source')}:/cmf-server/src",
            str(sif(root, server)),
            "sh",
            "-c",
            script,
        ],
        timeout=1800,
    )
    shutil.rmtree(scratch)
    marker.write_text(identity)


def secret_environment(component: dict[str, Any], private: dict[str, str]) -> dict[str, str]:
    """Pass a component's secrets through Apptainer's environment channel."""
    environment = {**os.environ}
    for name in component.get("secrets", []):
        environment["APPTAINERENV_" + name] = private[name]
    for key, value in component.get("environment", {}).items():
        environment["APPTAINERENV_" + key] = value
    return environment


def start(root: Path, manifest: dict[str, Any], private: dict[str, str]) -> None:
    """Start owned instances from their recorded SIFs, then wait for readiness."""
    images = json.loads((root / "images.json").read_text())
    for component in manifest["components"]:
        path = sif(root, component)
        if not path.is_file() or digest(path) != images[component["name"]]:
            raise ValueError("Reinstall: the installed image no longer matches its receipt")
        env = secret_environment(component, private)
        if owned_instance(root, component) is None:
            arguments = ["instance", "run", "--cleanenv", "--containall", "--writable-tmpfs"]
            if component.get("workdir"):
                arguments += ["--pwd", component["workdir"]]
            for relative, destination, readonly in component.get("mounts", []):
                source = owned_path(root, relative)
                if not readonly:
                    source.mkdir(parents=True, exist_ok=True)
                arguments += ["--bind", f"{source}:{destination}" + (":ro" if readonly else "")]
            launch = component.get("host_arguments", component.get("arguments", []))
            if component.get("entrypoint"):
                launch = [component["entrypoint"], *launch]
            apptainer(root, [*arguments, str(path), component["name"], *launch], env=env)
        check = component.get("host_check", component.get("check"))
        if check:
            deadline = time.monotonic() + int(component.get("ready_seconds", 90))
            while True:
                try:
                    apptainer(
                        root,
                        ["exec", "--cleanenv", f"instance://{component['name']}", *check],
                        env=env,
                    )
                    break
                except RuntimeError:
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"{component['role']} did not become ready") from None
                    time.sleep(1)
        initialize = component.get("host_initialize", component.get("initialize"))
        if initialize:
            apptainer(
                root,
                ["exec", "--cleanenv", f"instance://{component['name']}", *initialize],
                env=env,
            )


def running(root: Path, component: dict[str, Any]) -> bool:
    """Whether the owned instance for a component is up."""
    return owned_instance(root, component) is not None


def collect_logs(root: Path, manifest: dict[str, Any], private: dict[str, str]) -> None:
    """Copy bounded instance logs, redacted, into the service's logs folder."""
    payload = json.loads(apptainer(root, ["instance", "list", "--json"]) or "{}")
    rows = {row["instance"]: row for row in payload.get("instances") or []}
    for component in manifest["components"]:
        row = rows.get(component["name"])
        if row is None:
            continue
        text = ""
        for key in ("logOutPath", "logErrPath"):
            path = Path(row.get(key) or "")
            if path.is_file():
                text += path.read_text(errors="replace")[-8000:]
        for value in private.values():
            if value:
                text = text.replace(str(value), "[redacted]")
        (root / "logs").mkdir(exist_ok=True)
        (root / "logs" / (component["role"] + ".log")).write_text(text)


def cleanup(root: Path, manifest: dict[str, Any], private: dict[str, str]) -> None:
    """Stop only owned instances; data directories and SIFs are retained."""
    collect_logs(root, manifest, private)
    for component in reversed(manifest["components"]):
        if owned_instance(root, component) is None:
            continue
        apptainer(root, ["instance", "stop", "--timeout", "15", component["name"]])
        if owned_instance(root, component) is not None:
            raise RuntimeError("An owned monitoring instance is still running")


def delete_images(root: Path, manifest: dict[str, Any]) -> None:
    """Delete this deployment's SIFs and Apptainer cache after its instances stop."""
    if any(running(root, component) for component in manifest["components"]):
        raise ValueError("Stop the owned instances before deleting their images")
    for name in ("images", "downloads"):
        path = owned_path(root, f"containers/{name}")
        if path.exists():
            shutil.rmtree(path)
