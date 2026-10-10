"""CLIO's launcher for its private SearXNG, shipped as ``launch.py`` into the service directory.

Run by the service environment's Python (SearXNG's own uv environment, never CLIO's).
It serves the pinned, unmodified SearXNG source with Granian -- the WSGI server
SearXNG pins in ``requirements-server.txt`` at that commit, a compiled server with
wheels for Linux, macOS and Windows (uWSGI does not run on Windows) -- bound to the
loopback only.

The settings file CLIO writes holds no secret: the per-installation secret key reaches
SearXNG through ``SEARXNG_SECRET``, and engine API keys and an outgoing proxy (which
may carry credentials) are applied to SearXNG's in-memory settings after they load.
On Windows a CLIO-owned ``pwd`` stand-in (``clio_pwd_shim``) is installed before
SearXNG's ``searx.valkeydb`` does its unconditional ``import pwd``.

Granian may start its worker by spawning a fresh interpreter that re-runs this file
as ``__mp_main__``: :func:`boot` therefore runs for both names, :func:`serve` only for
``__main__``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
#: The CLIO-owned module that stands in for ``pwd`` on Windows.
PWD_SHIM = "clio_pwd_shim"


def _hook() -> types.ModuleType:
    """The lifecycle hook shipped beside this file (CLIO's own copy when run from CLIO)."""

    try:
        import searxng_hook  # noqa: PLC0415
    except ImportError:
        from clio_agent.gact.infrastructure import searxng_hook  # noqa: PLC0415
    return searxng_hook


def install_pwd_shim(root: Path, platform_name: str = os.name) -> bool:
    """On Windows only, make ``import pwd`` resolve to CLIO's stand-in; True when installed."""

    if platform_name != "nt" or "pwd" in sys.modules:
        return False
    path = root / f"{PWD_SHIM}.py"
    spec = importlib.util.spec_from_file_location(PWD_SHIM, path)
    if not path.is_file() or spec is None or spec.loader is None:
        raise RuntimeError("CLIO's pwd stand-in for Windows is missing; reinstall SearXNG")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["pwd"] = module
    return True


def freeze_version(manifest: dict[str, Any]) -> None:
    """Provide ``searx.version_frozen`` (the source is an archive, not a git checkout)."""

    pinned = manifest["searxng"]
    module = types.ModuleType("searx.version_frozen")
    module.VERSION_STRING = pinned["version"]  # type: ignore[attr-defined]
    module.VERSION_TAG = pinned["version"]  # type: ignore[attr-defined]
    module.DOCKER_TAG = pinned["version"].replace("+", "-")  # type: ignore[attr-defined]
    module.GIT_URL = "https://github.com/searxng/searxng"  # type: ignore[attr-defined]
    module.GIT_BRANCH = "master"  # type: ignore[attr-defined]
    sys.modules["searx.version_frozen"] = module


def outgoing_proxy(environ: MutableMapping[str, str]) -> str:
    """The host's HTTPS/HTTP proxy, if any (HPC egress is often proxied)."""

    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = environ.get(name, "").strip()
        if value:
            return value
    return ""


def settings_document(manifest: dict[str, Any], secret: str) -> dict[str, Any]:
    """The settings file: CLIO's generated settings plus this installation's instance name."""

    document = json.loads(json.dumps(manifest["searxng"]["settings"]))
    document.setdefault("general", {})["instance_name"] = _hook().instance_name(secret)
    return document


def apply_private_settings(
    settings: dict[str, Any], manifest: dict[str, Any], environ: MutableMapping[str, str]
) -> None:
    """Put credentials into SearXNG's loaded settings only (never into a file)."""

    proxy = outgoing_proxy(environ)
    if proxy:
        settings.setdefault("outgoing", {})["proxies"] = {"all://": [proxy]}
    keys = manifest["searxng"].get("engine_keys", {})
    for engine in settings.get("engines", []):
        variable = keys.get(engine.get("name"))
        if variable and environ.get(variable):
            setting = manifest["searxng"]["engine_key_settings"][engine["name"]]
            engine[setting] = environ[variable]


def boot(root: Path = ROOT, environ: MutableMapping[str, str] = os.environ) -> dict[str, Any]:
    """Prepare this interpreter to import SearXNG; returns the manifest."""

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    source = root / "environment" / "searxng-source"
    if not (source / "searx" / "webapp.py").is_file():
        raise RuntimeError("The SearXNG source is missing; reinstall SearXNG")
    if str(root) not in sys.path:
        sys.path.append(str(root))
    install_pwd_shim(root)
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    environ["PYTHONPATH"] = str(source)
    freeze_version(manifest)
    hook = _hook()
    secret = hook.read_secret(root)
    environ["SEARXNG_SECRET"] = secret
    path = root / "searxng-settings.yml"
    # JSON is YAML: SearXNG's yaml loader reads it as is.
    hook.write_private(path, json.dumps(settings_document(manifest, secret), indent=1))
    environ["SEARXNG_SETTINGS_PATH"] = str(path)
    import searx  # noqa: PLC0415 - loads the settings written above

    apply_private_settings(searx.settings, manifest, environ)
    return manifest


def serve(manifest: dict[str, Any]) -> None:
    """Serve SearXNG's WSGI app with Granian on the loopback."""

    from granian import Granian  # noqa: PLC0415
    from granian.constants import Interfaces  # noqa: PLC0415

    Granian(
        "searx.webapp:app",
        address="127.0.0.1",
        port=int(manifest["port"]),
        interface=Interfaces.WSGI,
        workers=1,
    ).serve()


if __name__ in {"__main__", "__mp_main__"}:
    MANIFEST = boot()
    if __name__ == "__main__":
        serve(MANIFEST)
