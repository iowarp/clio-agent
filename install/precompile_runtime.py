"""Precompile the Python modules used by the bundled GACT startup path.

``--package NAME`` also compiles every module of a first-party package. The server
runs with ``-B`` from a signed, read-only bundle, so a module imported lazily after
startup (agent tools, providers, the document runtime) would otherwise be compiled
in memory on every launch. Third-party packages are not compiled whole: all of them
add well over 100 MB to the bundle.

``--stdlib`` compiles the bundled standard library, as CPython's own installers do
(about 6 MB). Modules such as ``xml.dom``, ``plistlib``, ``statistics``, ``_strptime``
and the ``cp1252``/``mac_roman`` codecs are imported lazily, so the startup graph
misses them (#1979).

``--launch-path`` boots the real server (``clio_agent.gact --no-agent``, as
``runtime.json`` does) in a throwaway home, waits until it serves sessions, reads
every parameterless GET route the way the desktop does on first launch, and compiles
every module the server then had in ``sys.modules``. ``build_app()`` alone misses
the lifespan, uvicorn and route-time imports: about 1,400 third-party modules (dspy,
fastmcp, mcp, beartype, uvicorn, ...) that otherwise compile on every launch.
``-X importtime`` is not a substitute: it omits ``importlib.import_module`` loads.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import py_compile
import signal
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

#: GET routes that stream or long-poll; reading them would never return.
_STREAMING_ROUTE_MARKERS = ("stream", "events", "sse", "watch", "subscribe")

#: Runs the server as ``-m`` would and records its loaded module files: at exit,
#: and every few seconds in case the stop cannot be graceful.
_RECORDING_SERVER = """
import atexit, os, runpy, sys, threading
out = sys.argv.pop(1)
def record():
    files = {getattr(m, "__file__", None) for m in list(sys.modules.values())}
    temporary = f"{out}.{threading.get_ident()}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write("\\n".join(sorted(f for f in files if isinstance(f, str))))
    os.replace(temporary, out)
def keep_recording():
    while True:
        threading.Event().wait(5)
        record()
atexit.register(record)
threading.Thread(target=keep_recording, daemon=True).start()
sys.argv[0] = "clio_agent.gact"
runpy.run_module("clio_agent.gact", run_name="__main__", alter_sys=True)
"""


def _loaded_runtime_sources(python_root: Path) -> list[Path]:
    """Return loaded Python sources contained by ``python_root``."""

    sources: set[Path] = set()
    for module in tuple(sys.modules.values()):
        value = getattr(module, "__file__", None)
        if not isinstance(value, str):
            continue
        source = Path(value)
        if source.suffix not in {".py", ".pyw"}:
            continue
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(python_root)
        except (FileNotFoundError, ValueError):
            continue
        sources.add(resolved)
    return sorted(sources)


def precompile_startup_modules(python_root: Path, source_root: Path | None = None) -> int:
    """Build the app and write portable bytecode for its loaded modules."""

    root = python_root.resolve(strict=True)
    sys.dont_write_bytecode = True

    from clio_agent.gact.app import build_app

    build_app()
    sources = _loaded_runtime_sources(root)
    if source_root is not None:
        sources = sorted(
            set(sources) | set(_loaded_runtime_sources(source_root.resolve(strict=True)))
        )
    if not sources:
        raise RuntimeError(f"GACT startup loaded no Python sources under {root}")

    for source in sources:
        _compile(source, root if source_root is None else None)
    return len(sources)


def precompile_packages(python_root: Path, names: list[str]) -> int:
    """Write portable bytecode for every module of the named packages under ``python_root``."""

    root = python_root.resolve(strict=True)
    sources: set[Path] = set()
    for name in names:
        spec = importlib.util.find_spec(name)
        locations = list(spec.submodule_search_locations or []) if spec else []
        if not locations:
            raise RuntimeError(f"{name} is not an importable package")
        for location in locations:
            package = Path(location).resolve(strict=True)
            if not package.is_relative_to(root):
                # An editable source install is prepared through --source-root.
                continue
            sources.update(path for path in package.rglob("*.py") if path.is_file())
    for source in sorted(sources):
        _compile(source, root)
    return len(sources)


def precompile_stdlib(python_root: Path) -> tuple[int, int]:
    """Compile the bundled standard library; return (compiled, uncompilable) counts.

    CPython ships a few sources that are not valid Python (Tcl demo and test
    fixtures); those cannot be imported, so they are counted and skipped.
    """

    root = python_root.resolve(strict=True)
    stdlib = Path(sysconfig.get_path("stdlib")).resolve(strict=True)
    if not stdlib.is_relative_to(root):
        raise RuntimeError(f"the standard library {stdlib} is not inside {root}")
    site_dirs = {Path(sysconfig.get_path(key)).resolve() for key in ("purelib", "platlib")}
    compiled = skipped = 0
    for source in sorted(stdlib.rglob("*.py")):
        if any(source.is_relative_to(site) for site in site_dirs):
            continue
        try:
            _compile(source, root)
        except py_compile.PyCompileError:
            skipped += 1
            continue
        compiled += 1
    return compiled, skipped


def precompile_launch_path(python_root: Path, timeout: float = 300.0) -> int:
    """Boot the real server, read its API, and compile every module it imported."""

    root = python_root.resolve(strict=True)
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory(
        prefix="clio-precompile-", ignore_cleanup_errors=True
    ) as scratch:
        loaded = _boot_and_read_api(Path(scratch), timeout)
    if not loaded:
        raise RuntimeError("the launch-path boot recorded no loaded modules")
    sources: set[Path] = set()
    for name in loaded:
        if not name.endswith(".py"):
            continue  # extension modules and frozen modules have no bytecode to ship
        source = Path(name).resolve()
        if source.is_relative_to(root) and source.is_file():
            sources.add(source)
    for source in sorted(sources):
        _compile(source, root)
    return len(sources)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _http_get(url: str, timeout: float) -> tuple[int | None, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, b""
    except (OSError, ValueError):
        return None, b""


def _boot_and_read_api(home: Path, timeout: float) -> list[str]:
    """Run the bundled server in ``home``, read its GET API, stop it; return its module files."""

    port = _free_port()
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "CLIO_", "GACT_"))
    }
    environment.update(
        HOME=str(home),
        USERPROFILE=str(home),
        APPDATA=str(home / "AppData" / "Roaming"),
        LOCALAPPDATA=str(home / "AppData" / "Local"),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        XDG_CACHE_HOME=str(home / ".cache"),
        XDG_STATE_HOME=str(home / ".local" / "state"),
        CLIO_AGENT_HOME=str(home / "clio-agent"),
        # Never meet another clio-core daemon on this host's default port.
        CLIO_CORE_PORT=str(_free_port()),
        PYTHONDONTWRITEBYTECODE="1",
    )
    log_path = home / "launch-path.log"
    record_path = home / "launch-path-modules.txt"
    command = [
        sys.executable,
        *("-I", "-B", "-c", _RECORDING_SERVER, str(record_path), "--no-agent"),
        *("--host", "127.0.0.1", "--port", str(port)),
    ]
    options: dict[str, object] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    base = f"http://127.0.0.1:{port}"
    with log_path.open("wb") as log:
        server = subprocess.Popen(  # noqa: S603 - the bundled interpreter, fixed arguments
            command,
            cwd=home,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            **options,  # type: ignore[arg-type]
        )
        try:
            _wait_until_serving(server, base, timeout)
            _read_get_api(base, deadline=time.monotonic() + timeout)
        except BaseException:
            _stop(server)
            print(log_path.read_text(encoding="utf-8", errors="replace")[-4000:], file=sys.stderr)
            raise
        _stop(server)
    if not record_path.is_file():
        return []
    return record_path.read_text(encoding="utf-8").splitlines()


def _wait_until_serving(server: subprocess.Popen[bytes], base: str, timeout: float) -> None:
    """Wait for ``/v1/sessions``: health answers before the task runtime is ready."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"the launch-path server exited with {server.returncode}")
        if _http_get(f"{base}/v1/sessions", 5)[0] == 200:
            return
        time.sleep(0.2)
    raise RuntimeError(f"the launch-path server did not serve sessions within {timeout:.0f}s")


def _read_get_api(base: str, deadline: float) -> None:
    """Read every parameterless, non-streaming GET route once, as a first launch does."""

    status, body = _http_get(f"{base}/openapi.json", 60)
    if status != 200:
        raise RuntimeError(f"the launch-path server returned {status} for /openapi.json")
    paths = json.loads(body).get("paths", {})
    for path, operations in sorted(paths.items()):
        if time.monotonic() > deadline:
            return
        if "get" not in operations or "{" in path:
            continue
        if any(marker in path for marker in _STREAMING_ROUTE_MARKERS):
            continue
        _http_get(base + path, 10)


def _stop(server: subprocess.Popen[bytes]) -> None:
    """Stop the server gracefully so it stops the clio-core daemon it started."""

    if server.poll() is None:
        try:
            if os.name == "nt":
                server.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(server.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            server.wait(60)
            return
        except subprocess.TimeoutExpired:
            pass
    if os.name == "nt":
        subprocess.run(  # noqa: S603, S607 - system tool, fixed arguments
            ["taskkill", "/T", "/F", "/PID", str(server.pid)], capture_output=True, check=False
        )
    else:
        try:
            os.killpg(server.pid, signal.SIGKILL)
        except OSError:
            pass
    server.wait(30)


_REPLACE_ATTEMPTS = 5


def _current(source: Path, cache_path: Path) -> bool:
    """Whether ``cache_path`` is checked-hash bytecode of ``source`` as it is now."""

    try:
        with cache_path.open("rb") as stream:
            header = stream.read(16)
    except OSError:
        return False
    if len(header) < 16 or header[:4] != importlib.util.MAGIC_NUMBER:
        return False
    if int.from_bytes(header[4:8], "little") != 0b11:  # hash-based, checked
        return False
    return header[8:16] == importlib.util.source_hash(source.read_bytes())


def _compile(source: Path, root: Path | None) -> None:
    """Compile ``source`` beside itself, naming it relative to the bundled runtime."""

    cache_path = Path(importlib.util.cache_from_source(str(source)))
    # A source install's environment can arrive with its bytecode (install.ps1's
    # prebuilt dependencies): bytecode Python would accept as it is is kept.
    if root is None and _current(source, cache_path):
        return
    display_path = (
        Path("gact-runtime/python") / source.relative_to(root) if root is not None else source
    )
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            py_compile.compile(
                str(source),
                cfile=str(cache_path),
                dfile=display_path.as_posix(),
                doraise=True,
                # CHECKED_HASH, never UNCHECKED_HASH: the desktop upgrades this
                # runtime in place with ``uv pip install``, which rewrites sources
                # but cannot remove bytecode it did not write. Unchecked bytecode
                # then keeps executing the previous release's dependencies
                # (0.9.4.14 -> 0.9.4.15 stale clio_schemas ImportError). A source
                # hash is portable across relocation like the unchecked form, and
                # Python recompiles any module whose source changed.
                invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
            )
            return
        except PermissionError:
            # Windows refuses to replace a .pyc that another interpreter is
            # writing or holding open: the source installer compiles while
            # migrate and the managed-package step import the same checkout.
            # That interpreter's bytecode is valid for this source, so a file
            # that is still busy after a short wait is left as it is.
            if os.name != "nt":
                raise
            if attempt + 1 < _REPLACE_ATTEMPTS:
                time.sleep(0.05 * (attempt + 1))
    if not cache_path.exists():
        raise PermissionError(f"could not write bytecode for {source}: {cache_path}")


def main() -> int:
    """Run the bundled-runtime startup precompiler."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--python-root", required=True, type=Path)
    parser.add_argument("--source-root", type=Path, help="also prepare an editable source install")
    parser.add_argument(
        "--package",
        action="append",
        default=[],
        help="also compile every module of this package (repeatable)",
    )
    parser.add_argument(
        "--stdlib", action="store_true", help="also compile the bundled standard library"
    )
    parser.add_argument(
        "--launch-path",
        action="store_true",
        help="also boot the server, read its API, and compile every module it imported",
    )
    args = parser.parse_args()
    compiled = precompile_startup_modules(args.python_root, args.source_root)
    print(f"prepared {compiled} startup bytecode files")
    if args.package:
        packaged = precompile_packages(args.python_root, args.package)
        print(f"prepared {packaged} package bytecode files ({', '.join(args.package)})")
    if args.stdlib:
        compiled, skipped = precompile_stdlib(args.python_root)
        print(f"prepared {compiled} standard-library bytecode files ({skipped} not valid Python)")
    if args.launch_path:
        started = time.monotonic()
        launched = precompile_launch_path(args.python_root)
        print(
            f"prepared {launched} launch-path bytecode files "
            f"(booted and read the API in {time.monotonic() - started:.0f}s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
