"""Runtime setup, verification, reuse and policy have deterministic failure behavior."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.runtime import document_runtime as runtime
from clio_agent.runtime import document_venv
from clio_agent.tools.file_policy import FileAccessPolicy


def test_execution_host_inspection_leaves_workspace_untouched(tmp_path: Path) -> None:
    """Real inspection and a standard-library import check create no managed environment."""
    before = {path: path.read_bytes() if path.is_file() else None for path in tmp_path.rglob("*")}
    result = runtime.inspect_execution_runtime(tmp_path)
    assert result["inventory_scope"] == "execution_host"
    assert result["installs_dependencies"] is False
    assert result["python_argv"][-1] == "-B"
    assert runtime.inspect_python_imports(Path(result["python"]), ["csv", "json"]) == {
        "csv": {"status": "ready"},
        "json": {"status": "ready"},
    }
    assert {
        path: path.read_bytes() if path.is_file() else None for path in tmp_path.rglob("*")
    } == before


def test_python_upgrade_and_platform_change_use_distinct_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An upgrade cannot reuse a virtualenv built for another interpreter or host."""
    monkeypatch.setattr(runtime.sysconfig, "get_platform", lambda: "win-amd64")
    monkeypatch.setattr(
        runtime, "sys", SimpleNamespace(implementation=SimpleNamespace(cache_tag="cpython-312"))
    )
    old_cache = runtime._fingerprint()
    runtime.sys.implementation.cache_tag = "cpython-313"
    new_cache = runtime._fingerprint()
    assert new_cache != old_cache
    assert runtime._fingerprint() == new_cache
    monkeypatch.setattr(runtime.sysconfig, "get_platform", lambda: "linux-x86_64")
    assert runtime._fingerprint() != new_cache


def test_missing_uv_reports_execution_host_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FileAccessPolicy, "from_env", classmethod(lambda cls: FileAccessPolicy((tmp_path,)))
    )
    monkeypatch.setattr(runtime, "_document_uv", lambda: None)
    with pytest.raises(runtime.DocumentRuntimeError, match="uv is unavailable"):
        runtime.prepare_document_runtime(tmp_path, cache_root=tmp_path / "cache")


def test_import_preflight_checks_the_selected_interpreter_and_missing_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe real import behavior, including a deterministic unavailable module."""
    import subprocess
    import sys

    seen: list[str] = []

    def run(command: list[str], **kwargs: Any) -> str:
        seen.extend(command)
        return subprocess.check_output(command, text=True, cwd=kwargs["cwd"])

    monkeypatch.setattr(runtime, "_run", run)
    checks = runtime.inspect_python_imports(
        Path(sys.executable), ["json", "_clio_test_module_not_installed", "json"]
    )
    assert seen[0] == sys.executable
    assert checks["json"] == {"status": "ready"}
    assert checks["_clio_test_module_not_installed"]["status"] == "missing"
    assert len(checks) == 2
    with pytest.raises(ValueError, match="module names"):
        runtime.inspect_python_imports(Path(sys.executable), ["json;print('unsafe')"])


def test_python_failure_never_advertises_ready_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FileAccessPolicy, "from_env", classmethod(lambda cls: FileAccessPolicy((tmp_path,)))
    )
    monkeypatch.setattr(runtime, "_document_uv", lambda: "uv")

    def fail(cache: Path, uv: str, **kwargs: Any) -> tuple[Path, dict[str, Any]]:
        raise runtime.DocumentRuntimeError("offline: dependency missing")

    monkeypatch.setattr(runtime, "_python_runtime", fail)
    with pytest.raises(runtime.DocumentRuntimeError, match="offline"):
        runtime.prepare_document_runtime(tmp_path, cache_root=tmp_path / "cache")


def test_javascript_failure_keeps_verified_python_and_reports_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FileAccessPolicy, "from_env", classmethod(lambda cls: FileAccessPolicy((tmp_path,)))
    )
    monkeypatch.setattr(runtime, "_document_uv", lambda: "uv")
    inventory = {
        "native_tools": {"soffice": {"status": "missing"}, "tesseract": {"status": "missing"}},
        "python": "prepared-python",
    }
    monkeypatch.setattr(
        runtime, "_python_runtime", lambda cache, uv, **kwargs: (cache / "python", inventory)
    )
    monkeypatch.setattr(runtime, "_probe", lambda python, **kwargs: inventory)

    def fail(*args: Any, **kwargs: Any) -> tuple[Path, Path]:
        raise runtime.DocumentRuntimeError("module resolution failed")

    monkeypatch.setattr(runtime, "_javascript_runtime", fail)
    result = runtime.prepare_document_runtime(tmp_path, cache_root=tmp_path / "cache")
    assert result["status"] == "partial"
    assert result["javascript"]["status"] == "failed"
    assert result["python_argv"][-1] == "python"
    assert result["capabilities"]["office_render_recalculate"] == "missing"
    assert Path(result["output_directory"]).is_relative_to(tmp_path)
    assert Path(result["output_directory"]).is_relative_to(tmp_path / ".tmp")


def test_node_prepares_while_full_python_probe_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Node starts from the installed environment before the slow import probe ends."""
    from threading import Event

    monkeypatch.setattr(
        FileAccessPolicy, "from_env", classmethod(lambda cls: FileAccessPolicy((tmp_path,)))
    )
    monkeypatch.setattr(runtime, "_document_uv", lambda: "uv")
    discovered = {
        "node": str(tmp_path / "node"),
        "native_tools": {
            "soffice": {"status": "not_checked"},
            "tesseract": {"status": "not_checked"},
        },
    }
    probed = {
        "node": str(tmp_path / "node"),
        "native_tools": {"soffice": {"status": "available"}, "tesseract": {"status": "missing"}},
    }
    calls: list[tuple[bool, bool]] = []

    def python_runtime(
        cache: Path, uv: str, *, discovery_only: bool = False, defer_probe: bool = False
    ) -> Any:
        calls.append((discovery_only, defer_probe))
        return cache / "python", dict(discovered)

    node_started = Event()

    def probe(python: Path, **kwargs: Any) -> dict[str, Any]:
        assert node_started.wait(10), "the import probe blocked Node preparation"
        return dict(probed)

    def javascript(cache: Path, workspace: Path, inventory: dict[str, Any], **kwargs: Any) -> Any:
        assert inventory["node"] == discovered["node"]
        node_started.set()
        return tmp_path / "work", tmp_path / "pnpm"

    monkeypatch.setattr(runtime, "_python_runtime", python_runtime)
    monkeypatch.setattr(runtime, "_probe", probe)
    monkeypatch.setattr(runtime, "_javascript_runtime", javascript)
    monkeypatch.setattr(runtime, "publish_environment", lambda *args: None)
    result = runtime.prepare_document_runtime(tmp_path, cache_root=tmp_path / "cache")
    assert calls == [(True, True)]
    assert result["status"] == "ready" and result["javascript"]["status"] == "ready"
    assert result["capabilities"]["office_render_recalculate"] == "available"


def test_failed_full_probe_repairs_after_node_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe failure still gets the locked repair, once nothing runs from the environment."""
    monkeypatch.setattr(
        FileAccessPolicy, "from_env", classmethod(lambda cls: FileAccessPolicy((tmp_path,)))
    )
    monkeypatch.setattr(runtime, "_document_uv", lambda: "uv")
    tools = {"soffice": {"status": "missing"}, "tesseract": {"status": "missing"}}
    order: list[str] = []

    def python_runtime(cache: Path, uv: str, *, discovery_only: bool = False, **kwargs: Any) -> Any:
        order.append("discover" if discovery_only else "repair")
        return cache / "python", {
            "node": "node",
            "native_tools": tools,
            "repaired": not discovery_only,
        }

    def probe(python: Path, **kwargs: Any) -> dict[str, Any]:
        raise runtime.DocumentRuntimeError("pandas failed to import")

    def javascript(*args: Any, **kwargs: Any) -> Any:
        order.append("node")
        return tmp_path / "work", tmp_path / "pnpm"

    monkeypatch.setattr(runtime, "_python_runtime", python_runtime)
    monkeypatch.setattr(runtime, "_probe", probe)
    monkeypatch.setattr(runtime, "_javascript_runtime", javascript)
    monkeypatch.setattr(runtime, "publish_environment", lambda *args: None)
    result = runtime.prepare_document_runtime(tmp_path, cache_root=tmp_path / "cache")
    assert order == ["discover", "node", "repair"]
    assert result["repaired"] is True and result["javascript"]["status"] == "ready"


def test_node_failed_on_a_damaged_environment_retries_after_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Node.js starts before the environment is probed, so the repair gives it a second try."""
    monkeypatch.setattr(
        FileAccessPolicy, "from_env", classmethod(lambda cls: FileAccessPolicy((tmp_path,)))
    )
    monkeypatch.setattr(runtime, "_document_uv", lambda: "uv")
    tools = {"soffice": {"status": "missing"}, "tesseract": {"status": "missing"}}
    order: list[str] = []

    def python_runtime(cache: Path, uv: str, *, discovery_only: bool = False, **kwargs: Any) -> Any:
        order.append("discover" if discovery_only else "repair")
        return cache / "python", {"node": "node", "native_tools": tools}

    def probe(python: Path, **kwargs: Any) -> dict[str, Any]:
        raise runtime.DocumentRuntimeError("nodejs_wheel failed to import")

    def javascript(*args: Any, **kwargs: Any) -> Any:
        order.append("node")
        if order.count("node") == 1:
            raise runtime.DocumentRuntimeError("node.exe is damaged")
        return tmp_path / "work", tmp_path / "pnpm"

    monkeypatch.setattr(runtime, "_python_runtime", python_runtime)
    monkeypatch.setattr(runtime, "_probe", probe)
    monkeypatch.setattr(runtime, "_javascript_runtime", javascript)
    monkeypatch.setattr(runtime, "publish_environment", lambda *args: None)
    result = runtime.prepare_document_runtime(tmp_path, cache_root=tmp_path / "cache")
    assert order == ["discover", "node", "repair", "node"]
    assert result["status"] == "ready" and result["javascript"]["status"] == "ready"


def test_python_cache_reuse_still_probes_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    python = tmp_path / "python" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(runtime, "_probe", lambda path: {"python": str(path), "verified": True})
    result, inventory = runtime._python_runtime(tmp_path, "missing-uv")
    assert result == python
    assert inventory["verified"]


def _venv_from(tmp_path: Path, home: Path) -> Path:
    import os

    environment = tmp_path / "cache" / "python"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    (environment / "stale-marker").touch()
    (environment / "pyvenv.cfg").write_text(f"home = {home}\nversion_info = 3.14.0\n")
    return python


@pytest.mark.parametrize("old_copy", ["gone", "still-present"])
def test_moved_app_rebuilds_venv_on_its_own_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old_copy: str
) -> None:
    """A venv built by a translocated or older app copy is rebuilt, never reused or wedged.

    ``uv venv`` refuses an existing directory, so the stale one must go first; a still
    present old copy must not keep serving its interpreter either.
    """
    old_home = tmp_path / "Old.app" / "python" / "bin"
    if old_copy == "still-present":
        old_home.mkdir(parents=True)
    python = _venv_from(tmp_path, old_home)
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> str:
        assert not (python.parent.parent / "stale-marker").exists()
        commands.append(command)
        return ""

    monkeypatch.setattr(runtime, "_run", run)
    monkeypatch.setattr(runtime, "bundled_root", lambda: tmp_path)
    monkeypatch.setattr(
        runtime, "bundled_wheel_requirements", lambda *args: tmp_path / "requirements.txt"
    )
    monkeypatch.setattr(runtime, "_probe", lambda path: {"python": str(path)})
    runtime._python_runtime(tmp_path / "cache", "uv")
    assert commands[0][:2] == ["uv", "venv"]
    assert commands[0][commands[0].index("--python") + 1] == runtime.sys.executable
    assert "--reinstall" not in commands[1]


def test_venv_from_this_interpreter_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The base check accepts the running interpreter, so a warm cache never rebuilds."""
    import sys

    python = _venv_from(tmp_path, Path(sys.executable).resolve().parent)
    monkeypatch.setattr(runtime, "_run", lambda *args, **kwargs: pytest.fail("rebuilt"))
    monkeypatch.setattr(runtime, "_probe", lambda path: {"python": str(path)})
    result, _ = runtime._python_runtime(tmp_path / "cache", "uv")
    assert result == python
    assert (python.parent.parent / "stale-marker").exists()


@pytest.mark.parametrize(
    ("same_filesystem", "expected"), [(True, "shared"), (False, "private"), (None, "private")]
)
def test_document_sync_reuses_uv_cache_only_on_the_same_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_filesystem: bool | None, expected: str
) -> None:
    """The agent's install already downloaded numpy, matplotlib, lxml...: link, don't refetch.

    A cache on another drive (or one uv cannot name) would copy every file, so the
    document environment keeps its own cache beside it on the installation drive.
    """
    home = tmp_path / "clio-cache"
    shared = tmp_path / "uv-cache"
    monkeypatch.setattr(
        document_venv, "_uv_cache_dir", lambda uv: None if same_filesystem is None else shared
    )
    devices = {home: 1, shared: 1 if same_filesystem else 2}
    monkeypatch.setattr(document_venv, "_filesystem", lambda path: devices[path])
    commands: list[tuple[list[str], dict[str, str]]] = []

    def run(command: list[str], **kwargs: Any) -> str:
        commands.append((command, kwargs["env"]))
        return ""

    monkeypatch.setattr(runtime, "_run", run)
    monkeypatch.setattr(runtime, "bundled_root", lambda: None)
    monkeypatch.setattr(runtime, "_installer_wheel_root", lambda: None)
    monkeypatch.setattr(runtime, "_probe", lambda path: {"python": str(path)})
    runtime._python_runtime(home / "python-runtime", "uv")
    (command, env) = commands[-1]
    assert command[:2] == ["uv", "sync"]
    want = shared if expected == "shared" else home / "uv-document-cache"
    assert env["UV_CACHE_DIR"] == str(want)


def test_a_release_keeps_the_document_uv_cache_in_clio_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release lays the packages out from bundled wheels: its cache is CLIO's own, so
    ``uninstall --purge-caches`` removes it (~/.cache/uv kept ~500 MB of them)."""
    home = tmp_path / "clio-cache"
    monkeypatch.setattr(document_venv, "_uv_cache_dir", lambda uv: tmp_path / "uv-cache")
    monkeypatch.setattr(document_venv, "_filesystem", lambda path: 1)
    assert document_venv._document_uv_cache("uv", home, bundled=True) == home / "uv-document-cache"


def test_document_lock_matches_the_agent_lock_where_its_bounds_allow() -> None:
    """Shared packages resolve to the agent's versions, so one download serves both.

    Only the document stack's own upper bounds (``pillow<13``, ``filelock<4``) may hold a
    package at another version.
    """
    import tomllib

    root = Path(runtime.__file__).resolve().parents[3]
    agent_lock = root / "uv.lock"
    if not agent_lock.is_file():
        pytest.skip("the agent's uv.lock ships only in a source checkout")

    def versions(lock: Path) -> dict[str, str]:
        packages = tomllib.loads(lock.read_text(encoding="utf-8"))["package"]
        return {p["name"]: p["version"] for p in packages if "version" in p}

    agent = versions(agent_lock)
    document = versions(runtime.STACK_ROOT / "uv.lock")
    differing = {name for name in document if name in agent and agent[name] != document[name]}
    assert differing <= {"pillow", "filelock"}, (
        f"{sorted(differing)} differ from uv.lock: re-lock the document stack with these "
        "pinned as [tool.uv] constraint-dependencies, then drop the constraints and lock again"
    )


def test_execution_discovery_probes_current_files_each_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No remembered answer can hide a changed or missing module."""
    import os

    python = tmp_path / "python" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    calls = 0

    def probe(path: Path, *, discovery_only: bool = False) -> dict[str, Any]:
        nonlocal calls
        assert path == python
        assert discovery_only
        calls += 1
        return {"current_revision": calls}

    monkeypatch.setattr(runtime, "_probe", probe)
    _, first = runtime._python_runtime(tmp_path, "unused-uv", discovery_only=True)
    _, second = runtime._python_runtime(tmp_path, "unused-uv", discovery_only=True)
    assert first["current_revision"] == 1
    assert second["current_revision"] == 2


def test_install_locates_node_without_a_discovery_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install's full probe follows, so finding Node.js starts no process."""
    import os
    import sysconfig

    python = tmp_path / "python" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    base = str(tmp_path / "python")
    purelib = sysconfig.get_path("purelib", "venv", vars={"base": base, "platbase": base})
    node = Path(purelib) / "nodejs_wheel" / ("node.exe" if os.name == "nt" else "bin/node")
    node.parent.mkdir(parents=True)
    node.touch()
    monkeypatch.setattr(runtime, "_built_for_this_interpreter", lambda environment: True)
    monkeypatch.setattr(runtime, "_probe", lambda *args, **kwargs: pytest.fail("probed"))
    result, inventory = runtime._python_runtime(
        tmp_path, "unused-uv", discovery_only=True, defer_probe=True
    )
    assert result == python
    assert inventory == {"node": str(node)}


def test_install_probes_when_node_cannot_be_located(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown layout falls back to the discovery probe rather than guessing."""
    import os

    python = tmp_path / "python" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(runtime, "_built_for_this_interpreter", lambda environment: True)
    monkeypatch.setattr(
        runtime, "_probe", lambda path, *, discovery_only=False: {"discovered": discovery_only}
    )
    _, inventory = runtime._python_runtime(
        tmp_path, "unused-uv", discovery_only=True, defer_probe=True
    )
    assert inventory == {"discovered": True}


def test_execution_probe_requests_light_inventory_and_still_checks_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execution discovery must not accept a changed installed package version."""

    def run(command: list[str], **kwargs: Any) -> str:
        assert command[-1] == "--execution-only"
        return json.dumps({"packages": {"numpy": {"version": "wrong"}}})

    monkeypatch.setattr(runtime, "_run", run)
    with pytest.raises(runtime.DocumentRuntimeError, match="differs from its locked version"):
        runtime._probe(Path("env/bin/python"), discovery_only=True)


def test_javascript_preserves_changed_task_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    package = cache / "javascript-tools" / "node_modules" / "pnpm"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"version": runtime.PNPM_VERSION, "bin": {"pnpm": "pnpm"}})
    )
    (package / "pnpm").touch()
    work = tmp_path / ".tmp" / "clio-documents" / "javascript"
    work.mkdir(parents=True)
    custom = '{"dependencies":{"custom":"1.0.0"}}'
    (work / "package.json").write_text(custom)
    monkeypatch.setattr(runtime, "_run", lambda *args, **kwargs: runtime.PNPM_VERSION)
    with pytest.raises(runtime.DocumentRuntimeError, match="manifest was changed"):
        runtime._javascript_runtime(cache, tmp_path, {"node": "node"})
    assert (work / "package.json").read_text() == custom


@pytest.mark.parametrize("damage", ["invalid-json", "wrong-version", "wrong-executable"])
def test_damaged_pnpm_gets_one_verified_pinned_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    package = tmp_path / "cache/javascript-tools/node_modules/pnpm"
    package.mkdir(parents=True)
    metadata = package / "package.json"
    metadata.write_text(
        "invalid"
        if damage == "invalid-json"
        else json.dumps(
            {
                "version": "old" if damage == "wrong-version" else runtime.PNPM_VERSION,
                "bin": {"pnpm": "pnpm"},
            }
        )
    )
    (package / "pnpm").touch()
    node = tmp_path / "managed node" / "node.exe"
    npm = node.parent / "lib/node_modules/npm/bin/npm-cli.js"
    npm.parent.mkdir(parents=True)
    npm.touch()
    work = tmp_path / ".tmp/clio-documents/javascript"
    (work / "node_modules").mkdir(parents=True)
    installs: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> str:
        if "--prefix" in command:
            installs.append(command)
            metadata.write_text(
                json.dumps({"version": runtime.PNPM_VERSION, "bin": {"pnpm": "pnpm"}})
            )
        if "--version" in command:
            return "old" if damage == "wrong-executable" and not installs else runtime.PNPM_VERSION
        return ""

    monkeypatch.setattr(runtime, "_run", run)
    prepared, pnpm = runtime._javascript_runtime(tmp_path / "cache", tmp_path, {"node": str(node)})
    assert prepared == work
    assert pnpm == package / "pnpm"
    assert len(installs) == 1
    assert f"pnpm@{runtime.PNPM_VERSION}" in installs[0]
    assert installs[0][:2] == [str(node), str(npm)]


def test_failed_helper_exit_retains_manifest_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from clio_agent.runtime import sandbox

    seen: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> str:
        seen.update(kwargs)
        assert command == ["confined-python", "helper.py", "render", "input.docx"]
        return json.dumps({"status": "failed", "manifest": "output/manifest.json"})

    monkeypatch.setattr(runtime, "_run", run)
    monkeypatch.setattr(runtime, "prepare_office_runtime", lambda: "prepared-soffice")
    monkeypatch.setattr(
        sandbox,
        "wrap_confined",
        lambda command, args, **kwargs: SimpleNamespace(
            command="confined-python",
            args=args,
            env_overlay={"TEMP": "backend-cache"},
            release=lambda: None,
        ),
    )
    result = runtime.run_document_helper(
        {
            "helper_argv": ["python", "helper.py"],
            "shell_environment": {"TEMP": str(tmp_path / ".tmp")},
        },
        ["render", "input.docx"],
        cwd=tmp_path,
    )
    assert result["status"] == "failed"
    assert seen["accepted_exit_codes"] == (0, 1)
    assert seen["env"]["TEMP"] == str(tmp_path / ".tmp")
    assert seen["env"]["CLIO_DOCUMENT_SOFFICE"] == "prepared-soffice"
    assert seen["env"]["CLIO_DOCUMENT_CONFINED"] == "1"


def _npm_tarball(path: Path, files: dict[str, str]) -> str:
    import hashlib
    import io
    import tarfile

    with tarfile.open(path, "w:gz") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(f"package/{name}")
            member.size = len(data.encode())
            archive.addfile(member, io.BytesIO(data.encode()))
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("downloads", ["valid", "wrong-version", "tampered", "absent"])
def test_installer_downloaded_pnpm_replaces_the_registry_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, downloads: str
) -> None:
    """The installer's pinned pnpm tarballs unpack offline; anything else uses the registry."""
    folder = tmp_path / "installer-downloads"
    folder.mkdir()
    host = "@pnpm/exe.darwin-arm64"
    monkeypatch.setattr(runtime, "_pnpm_host_package", lambda: host)
    version = runtime.PNPM_VERSION if downloads != "wrong-version" else "old"
    wrapper = {
        "package.json": json.dumps({"version": version, "bin": {"pnpm": "pnpm"}}),
        "pnpm": "#!/usr/bin/env node",
    }
    pins = {
        "pnpm": _npm_tarball(folder / runtime.pnpm_tarball("pnpm"), wrapper),
        host: _npm_tarball(
            folder / runtime.pnpm_tarball(host), {"package.json": "{}", "pnpm": "native"}
        ),
    }
    monkeypatch.setattr(runtime, "PNPM_PACKAGES", pins)
    if downloads == "tampered":
        (folder / runtime.pnpm_tarball(host)).write_bytes(b"changed")
    if downloads != "absent":
        monkeypatch.setenv("CLIO_INSTALL_DOWNLOADS", str(folder))
    node = tmp_path / "node" / "bin" / "node"
    npm = tmp_path / "node/lib/node_modules/npm/bin/npm-cli.js"
    npm.parent.mkdir(parents=True)
    npm.touch()
    modules = tmp_path / "cache/javascript-tools/node_modules"
    installs: list[list[str]] = []
    messages: list[str] = []

    def run(command: list[str], **kwargs: Any) -> str:
        if "--prefix" in command:
            installs.append(command)
            (modules / "pnpm").mkdir(parents=True, exist_ok=True)
            (modules / "pnpm/pnpm").write_text("registry")
            (modules / "pnpm/package.json").write_text(
                json.dumps({"version": runtime.PNPM_VERSION, "bin": {"pnpm": "pnpm"}})
            )
        return runtime.PNPM_VERSION if "--version" in command else ""

    monkeypatch.setattr(runtime, "_run", run)
    _, pnpm = runtime._javascript_runtime(
        tmp_path / "cache", tmp_path, {"node": str(node)}, progress=messages.append
    )
    assert pnpm == modules / "pnpm/pnpm"
    if downloads == "valid":
        assert installs == []
        assert pnpm.read_text() == "#!/usr/bin/env node"
        assert (modules / host / "pnpm").read_text() == "native"
    else:
        assert len(installs) == 1
        assert pnpm.read_text() == "registry"
        assert any("could not be used" in m for m in messages) == (downloads == "wrong-version")
