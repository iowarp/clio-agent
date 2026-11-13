"""Installation is complete only after both package sets and Office are available."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Barrier, Event
from typing import Any

import pytest

from clio_agent.runtime import document_install as installer


@pytest.mark.parametrize("javascript", ["ready", "failed"])
def test_installer_records_only_complete_provisioning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, javascript: str
) -> None:
    cache = tmp_path / "cache"
    (cache / "locked").mkdir(parents=True)
    result: dict[str, Any] = {
        "status": "ready",
        "runtime_id": "locked",
        "javascript": {"status": javascript},
        "native_tools": {},
        "capabilities": {},
    }
    monkeypatch.setattr(installer, "prepare_document_runtime", lambda *a, **kw: result)
    monkeypatch.setattr(installer, "prepare_office_runtime", lambda **_: "private soffice")
    monkeypatch.setattr(installer, "ensure_github_cli", lambda: tmp_path / "gh")
    monkeypatch.setattr(installer, "ensure_ripgrep", lambda: Path("/usr/bin/rg"))
    monkeypatch.setattr(installer, "ensure_tectonic", lambda: Path("/usr/bin/tectonic"))
    monkeypatch.setattr(
        installer, "ensure_bundled_codex_windows_helpers", lambda: {"status": "available"}
    )
    monkeypatch.setattr(
        installer, "prepare_existing_windows_fence", lambda **kwargs: {"status": "available"}
    )
    stages: list[str] = []
    receipt = cache / "locked" / "installed.json"
    if javascript == "failed":
        with pytest.raises(installer.DocumentRuntimeError, match="Node/pnpm"):
            installer.install_document_runtime(
                tmp_path / "workspace", cache_root=cache, progress=stages.append
            )
        assert not receipt.exists()
        assert "All managed runtime packages are installed and verified." not in stages
    else:
        installed = installer.install_document_runtime(
            tmp_path / "workspace", cache_root=cache, progress=stages.append
        )
        assert installed["capabilities"]["office_render_recalculate"] == "available"
        assert installed["native_tools"]["gh"]["authentication"] == "clio_account"
        assert (
            json.loads(receipt.read_text())["native_tools"]["soffice"]["path"] == "private soffice"
        )
        assert set(stages[:-1]) == {
            "Preparing and checking the Office renderer...",
            "Office rendering is ready.",
            "Preparing and checking the GitHub command-line tool...",
            "GitHub command-line tool is ready.",
            "Preparing and checking ripgrep for file search...",
            "ripgrep is ready.",
            "Preparing and checking Tectonic for LaTeX builds...",
            "Tectonic is ready.",
            "Preparing and checking protected execution...",
        }
        assert stages[-1] == "All managed runtime packages are installed and verified."


def test_installer_overlaps_independent_work_and_joins_before_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    (cache / "locked").mkdir(parents=True)
    receipt = cache / "locked" / "installed.json"
    started = Barrier(3, timeout=5)
    packages_done = Event()
    office_done = Event()
    github_done = Event()

    def packages(*args: Any, **kwargs: Any) -> dict[str, Any]:
        started.wait()
        assert not receipt.exists()
        packages_done.set()
        return {
            "status": "ready",
            "runtime_id": "locked",
            "javascript": {"status": "ready"},
            "native_tools": {},
            "capabilities": {},
        }

    def office(**_: object) -> str:
        started.wait()
        assert packages_done.wait(5)
        assert not receipt.exists()
        office_done.set()
        return "private soffice"

    def github() -> Path:
        started.wait()
        assert packages_done.wait(5)
        assert not receipt.exists()
        github_done.set()
        return tmp_path / "gh"

    monkeypatch.setattr(installer, "prepare_document_runtime", packages)
    monkeypatch.setattr(installer, "prepare_office_runtime", office)
    monkeypatch.setattr(installer, "ensure_github_cli", github)
    monkeypatch.setattr(installer, "ensure_ripgrep", lambda: Path("/usr/bin/rg"))
    monkeypatch.setattr(installer, "ensure_tectonic", lambda: Path("/usr/bin/tectonic"))
    monkeypatch.setattr(
        installer, "ensure_bundled_codex_windows_helpers", lambda: {"status": "available"}
    )
    monkeypatch.setattr(
        installer, "prepare_existing_windows_fence", lambda **kwargs: {"status": "available"}
    )
    installer.install_document_runtime(tmp_path / "workspace", cache_root=cache)
    assert office_done.is_set() and github_done.is_set()
    assert receipt.exists()


def test_installer_reports_each_sub_jobs_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "locked").mkdir()

    def packages(*args: Any, timings: dict[str, float] | None = None, **kwargs: Any) -> Any:
        assert timings is not None
        timings["python_environment"] = 1.0
        return {
            "status": "ready",
            "runtime_id": "locked",
            "javascript": {"status": "ready"},
            "native_tools": {},
            "capabilities": {},
        }

    monkeypatch.setattr(installer, "prepare_document_runtime", packages)
    monkeypatch.setattr(installer, "prepare_office_runtime", lambda **_: "soffice")
    monkeypatch.setattr(installer, "ensure_github_cli", lambda: tmp_path / "gh")
    monkeypatch.setattr(installer, "ensure_ripgrep", lambda: Path("/usr/bin/rg"))
    monkeypatch.setattr(installer, "ensure_tectonic", lambda: Path("/usr/bin/tectonic"))
    monkeypatch.setattr(
        installer,
        "prepare_existing_windows_fence",
        lambda **kwargs: {"status": "available", "implementation": "mxc"},
    )
    timings: dict[str, float] = {}
    installer.install_document_runtime(tmp_path / "workspace", cache_root=tmp_path, timings=timings)
    assert set(timings) == {
        "protect_runtime",
        "office",
        "gh",
        "rg",
        "tectonic",
        "packages",
        "python_environment",
        "protection",
        "receipt",
    }
    assert all(isinstance(seconds, float) and seconds >= 0 for seconds in timings.values())


@pytest.mark.parametrize("failed_step", ["office", "github", "python"])
def test_installer_failure_waits_for_other_work_without_publishing_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_step: str
) -> None:
    cache = tmp_path / "cache"
    (cache / "locked").mkdir(parents=True)
    started = Barrier(3, timeout=5)
    finished: list[str] = []

    def step(name: str) -> None:
        started.wait()
        finished.append(name)
        if name == failed_step:
            raise RuntimeError(f"{name} failed")

    def packages(*args: Any, **kwargs: Any) -> dict[str, Any]:
        step("python")
        return {
            "status": "ready",
            "runtime_id": "locked",
            "javascript": {"status": "ready"},
            "native_tools": {},
            "capabilities": {},
        }

    def office(**_: object) -> str:
        step("office")
        return "private soffice"

    def github() -> Path:
        step("github")
        return tmp_path / "gh"

    monkeypatch.setattr(installer, "prepare_document_runtime", packages)
    monkeypatch.setattr(installer, "prepare_office_runtime", office)
    monkeypatch.setattr(installer, "ensure_github_cli", github)
    monkeypatch.setattr(installer, "ensure_ripgrep", lambda: Path("/usr/bin/rg"))
    monkeypatch.setattr(installer, "ensure_tectonic", lambda: Path("/usr/bin/tectonic"))
    stages: list[str] = []
    with pytest.raises(RuntimeError, match=f"{failed_step} failed"):
        installer.install_document_runtime(
            tmp_path / "workspace", cache_root=cache, progress=stages.append
        )
    assert set(finished) == {"python", "office", "github"}
    assert not (cache / "locked" / "installed.json").exists()
    assert "All managed runtime packages are installed and verified." not in stages


@pytest.mark.parametrize("setup", [True, False])
@pytest.mark.parametrize("failed", [True, False])
def test_installer_explicit_setup_is_forwarded_and_failed_setup_has_no_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setup: bool, failed: bool
) -> None:
    (tmp_path / "locked").mkdir()
    monkeypatch.setattr(
        installer,
        "prepare_document_runtime",
        lambda *args, **kwargs: {
            "status": "ready",
            "runtime_id": "locked",
            "javascript": {"status": "ready"},
            "native_tools": {},
            "capabilities": {},
        },
    )
    monkeypatch.setattr(installer, "prepare_office_runtime", lambda **_: "soffice")
    monkeypatch.setattr(installer, "ensure_github_cli", lambda: tmp_path / "gh")
    monkeypatch.setattr(installer, "ensure_ripgrep", lambda: Path("/usr/bin/rg"))
    monkeypatch.setattr(installer, "ensure_tectonic", lambda: Path("/usr/bin/tectonic"))

    def fence(**kwargs: Any) -> dict[str, str]:
        assert kwargs["allow_elevation"] is setup
        if failed:
            raise installer.DocumentRuntimeError("Windows approval declined")
        return {"status": "available", "implementation": "mxc"}

    def helpers() -> None:
        pytest.fail("Verified MXC must not download unused legacy helpers")

    monkeypatch.setattr(installer, "prepare_existing_windows_fence", fence)
    monkeypatch.setattr(installer, "ensure_bundled_codex_windows_helpers", helpers)
    if failed:
        with pytest.raises(installer.DocumentRuntimeError, match="approval declined"):
            installer.install_document_runtime(
                tmp_path / "workspace", cache_root=tmp_path, setup_protected_execution=setup
            )
    else:
        result = installer.install_document_runtime(
            tmp_path / "workspace", cache_root=tmp_path, setup_protected_execution=setup
        )
        assert result["native_tools"]["codex_windows_helpers"]["status"] == "not_required"
    assert (tmp_path / "locked/installed.json").exists() is not failed


def test_install_bounds_the_mcp_cache_with_a_typed_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B4: the MCP cache is pruned at install or upgrade, never at server start."""
    from clio_agent.tools import mcp_cache

    monkeypatch.setattr(mcp_cache, "prune_owned_mcp_cache", lambda: None)
    assert installer.prune_after_install() == {
        "status": "skipped",
        "reason": "mcp_cache_prune_skipped_live",
    }

    def broken() -> None:
        raise PermissionError("cache is read-only")

    monkeypatch.setattr(mcp_cache, "prune_owned_mcp_cache", broken)
    outcome = installer.prune_after_install()
    assert outcome["status"] == "failed" and outcome["reason"] == "mcp_cache_prune_failed"


def test_bootstrap_prepares_real_package_stage_without_fence_or_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result: dict[str, Any] = {
        "runtime_id": "locked",
        "javascript": {"status": "ready"},
        "native_tools": {},
        "capabilities": {},
    }
    monkeypatch.setattr(installer, "prepare_document_runtime", lambda *a, **kw: result)
    monkeypatch.setattr(installer, "prepare_office_runtime", lambda **_: "private soffice")
    monkeypatch.setattr(installer, "ensure_github_cli", lambda: tmp_path / "gh")
    monkeypatch.setattr(installer, "ensure_ripgrep", lambda: Path("/usr/bin/rg"))
    monkeypatch.setattr(installer, "ensure_tectonic", lambda: Path("/usr/bin/tectonic"))

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Cache preparation must leave protection and receipt to the installer")

    monkeypatch.setattr(installer, "prepare_protected_execution", unexpected)
    monkeypatch.setattr(installer, "save_install_receipt", unexpected)
    prepared = installer.prepare_managed_packages(tmp_path / "workspace", cache_root=tmp_path)
    assert prepared["capabilities"]["office_render_recalculate"] == "available"
    assert "protected_execution" not in prepared["native_tools"]


def test_store_upgrade_runs_while_packages_are_prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``migrate`` overlaps the package step; the summary waits for both.

    clio-core is entered from the main thread: a worker thread's native handles outlive
    it and nanobind reports them leaked at exit.
    """
    import sys
    import threading

    both_running = Barrier(3, timeout=10)

    def install(workspace: Path, **kwargs: Any) -> dict[str, Any]:
        both_running.wait()
        return {"status": "ready", "runtime_id": "locked", "native_tools": {"soffice": {}}}

    def migrate(args: Any, emit: Any) -> dict[str, Any]:
        assert threading.current_thread() is threading.main_thread()
        both_running.wait()
        emit("migrate status=done ran=0 seconds=0.000")
        return {"status": "done", "seconds": 0.0}

    def browser(args: Any, emit: Any) -> dict[str, Any]:
        both_running.wait()
        return {"status": "ready", "seconds": 0.0}

    monkeypatch.setattr(installer, "install_document_runtime", install)
    monkeypatch.setattr(installer, "_run_store_migrations", migrate)
    monkeypatch.setattr(installer, "_prepare_browser", browser)
    monkeypatch.setattr(
        installer, "_prepare_default_marketplace", lambda args, emit: {"status": "ready"}
    )
    monkeypatch.setattr(installer, "_prewarm_runtime_files", lambda args: {"status": "done"})
    monkeypatch.setattr(installer, "prune_after_install", lambda: {"status": "pruned"})
    monkeypatch.setattr(
        installer, "collect_snapshots_after_install", lambda: {"status": "collected"}
    )
    monkeypatch.setattr(
        sys, "argv", ["document_install", "--workspace", str(tmp_path / "workspace")]
    )
    installer._main()
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["status"] == "ready"
    assert summary["store_migrations"] == {"status": "done", "seconds": 0.0}
    assert summary["browser"] == {"status": "ready", "seconds": 0.0}
    assert summary["marketplace"] == {"status": "ready"}
    assert summary["prewarm"] == {"status": "done"}


def test_prewarm_opens_bytecode_sources_and_native_libraries_only_on_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first start no longer meets every freshly written code file cold."""
    import sys
    from argparse import Namespace

    lib = tmp_path / "Lib"
    site = lib / "site-packages"
    cache = site / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    tag = sys.implementation.cache_tag
    (site / "pkg" / "mod.py").write_text("x = 1\n")
    (cache / f"mod.{tag}.pyc").write_bytes(b"\0")
    (cache / "mod.other-tag.pyc").write_bytes(b"\0")
    (site / "pkg" / "_native.pyd").write_bytes(b"\0")
    (site / "pkg" / "notes.txt").write_text("not code")
    (site / "pkg" / "unused.py").write_text("y = 2\n")
    stdlib = tmp_path / "python314.zip"
    stdlib.write_bytes(b"PK")
    home = tmp_path.parent / f"{tmp_path.name}-home"
    (home / "project").mkdir(parents=True)
    (home / "project" / "big.zip").write_bytes(b"PK")
    (home / "project" / "tool.py").write_text("z = 3\n")
    for name in ("prefix", "base_prefix", "exec_prefix", "base_exec_prefix"):
        monkeypatch.setattr(sys, name, str(tmp_path))
    # ``python -m`` run from a home directory puts that directory first on the path.
    monkeypatch.setattr(sys, "path", [str(home), str(stdlib), str(lib), str(site)])
    # Without a recorded server import list the whole runtime is walked.
    monkeypatch.setattr(installer, "SERVER_IMPORTS", tmp_path / "no-server-imports.txt")

    files = sorted(
        Path(p).relative_to(tmp_path).as_posix() for p in installer._runtime_code_files()
    )
    assert files == [
        "Lib/site-packages/pkg/__pycache__/" + f"mod.{tag}.pyc",
        "Lib/site-packages/pkg/_native.pyd",
        "Lib/site-packages/pkg/mod.py",
        "python314.zip",
    ]
    launch = installer._prewarm_runtime_files(Namespace(reuse_installed=True))
    assert launch == {"status": "skipped", "reason": "reuse_installed"}
    install = installer._prewarm_runtime_files(Namespace(reuse_installed=False))
    assert install["status"] == "done"
    assert (install["files"], install["unreadable"]) == (4, 0)


def test_prewarm_opens_only_the_recorded_server_imports_when_they_ship(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded list opens just those files (and each module's bytecode), looked up in
    the most specific import-path folder; lines this install lacks are skipped."""
    import sys

    lib = tmp_path / "Lib"
    site = lib / "site-packages"
    (site / "pkg" / "__pycache__").mkdir(parents=True)
    tag = sys.implementation.cache_tag
    (site / "pkg" / "mod.py").write_text("x = 1\n")
    (site / "pkg" / "__pycache__" / f"mod.{tag}.pyc").write_bytes(b"\0")
    (site / "pkg" / "unused.py").write_text("y = 2\n")
    (lib / "os.py").write_text("")
    stdlib = tmp_path / "python314.zip"
    stdlib.write_bytes(b"PK")
    listed = tmp_path / "server_imports.txt"
    listed.write_text("# recorded\nos.py\npkg/mod.py\npkg/_absent.pyd\n", encoding="utf-8")
    monkeypatch.setattr(installer, "SERVER_IMPORTS", listed)
    monkeypatch.setattr(sys, "path", [str(stdlib), str(lib), str(site)])

    files = [Path(p).relative_to(tmp_path).as_posix() for p in installer._runtime_code_files()]
    assert files == [
        "python314.zip",
        "Lib/os.py",
        "Lib/site-packages/pkg/mod.py",
        "Lib/site-packages/pkg/__pycache__/" + f"mod.{tag}.pyc",
    ]


def test_default_marketplace_is_installed_by_an_install_but_never_by_a_launch_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server's start finds the default marketplace current instead of fetching it."""
    from argparse import Namespace

    from clio_agent.plugins.service import DEFAULT_MARKETPLACE, PluginService

    lines: list[str] = []
    launch = installer._prepare_default_marketplace(Namespace(reuse_installed=True), lines.append)
    assert launch == {"status": "skipped", "reason": "reuse_installed"}
    assert DEFAULT_MARKETPLACE not in PluginService().store.marketplaces()
    install = installer._prepare_default_marketplace(Namespace(reuse_installed=False), lines.append)
    assert install["status"] == "ready", install
    assert lines[-1] == "The default agents are installed."
    assert PluginService().ensure_default_marketplace() is None


def _done(value: dict[str, Any]) -> Any:
    from concurrent.futures import Future

    future: Future[dict[str, Any]] = Future()
    future.set_result(value)
    return future


def test_tool_servers_wait_for_the_installer_kit_then_build_in_parallel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The agents' clio-kit servers are built at install, after the installer's clio-kit."""
    from argparse import Namespace

    from clio_agent.tools import mcp_prepare

    signal = tmp_path / "kit-ready.json"
    kit_bin = tmp_path / "kit-bin"
    monkeypatch.setenv(installer.KIT_READY_ENV, str(signal))
    monkeypatch.setenv("PATH", "base")
    monkeypatch.setattr(installer, "_KIT_READY_POLL_S", 0.01)
    servers = {"hdf5": object(), "parquet": object(), "adios": object()}
    monkeypatch.setattr(installer, "_carried_kit_servers", lambda: servers)
    seen_path: list[str] = []
    both = Barrier(len(servers), timeout=5)

    def provision(name: str, spec: Any) -> str:
        seen_path.append(__import__("os").environ["PATH"])
        both.wait()  # every server builds at once
        if name == "adios":
            raise mcp_prepare.PrepareError(mcp_prepare.PROVISION_NETWORK, "offline")
        return mcp_prepare.PROVISIONED

    monkeypatch.setattr(mcp_prepare, "provision", provision)
    args = Namespace(prepare_tool_servers=True, reuse_installed=False)
    started = Event()

    def write_signal() -> None:
        started.wait(5)
        signal.write_text(
            json.dumps({"status": "ready", "path": [str(kit_bin)], "env": {"UV_TOOL_DIR": "t"}}),
            encoding="utf-8",
        )

    from threading import Thread

    writer = Thread(target=write_signal)
    writer.start()
    started.set()
    lines: list[str] = []
    result = installer._prepare_tool_servers(args, _done({"status": "ready"}), lines.append)
    writer.join()
    assert result["status"] == "failed"
    assert result["servers"]["hdf5"]["status"] == mcp_prepare.PROVISIONED
    assert result["servers"]["adios"]["reason"] == mcp_prepare.PROVISION_NETWORK
    assert all(path.startswith(str(kit_bin)) for path in seen_path)
    assert __import__("os").environ["UV_TOOL_DIR"] == "t"
    assert lines[-1].endswith("adios")


def test_tool_servers_are_skipped_without_the_flag_kit_or_marketplace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from argparse import Namespace

    calls: list[str] = []
    monkeypatch.setattr(installer, "_carried_kit_servers", lambda: calls.append("x") or {})
    ready = _done({"status": "ready"})
    off = installer._prepare_tool_servers(
        Namespace(prepare_tool_servers=False, reuse_installed=False), ready, print
    )
    assert off == {"status": "skipped", "reason": "not_requested"}
    no_market = installer._prepare_tool_servers(
        Namespace(prepare_tool_servers=True, reuse_installed=False),
        _done({"status": "failed"}),
        print,
    )
    assert no_market["reason"] == "tool_servers_marketplace_unavailable"
    signal = tmp_path / "kit-ready.json"
    signal.write_text(json.dumps({"status": "failed", "path": [], "env": {}}), encoding="utf-8")
    monkeypatch.setenv(installer.KIT_READY_ENV, str(signal))
    no_kit = installer._prepare_tool_servers(
        Namespace(prepare_tool_servers=True, reuse_installed=False), ready, print
    )
    assert no_kit["reason"] == "tool_servers_kit_unavailable"
    assert calls == []


def test_browser_is_fetched_by_an_install_but_never_by_a_launch_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argparse import Namespace

    from clio_agent.gact.browser import install as browser_install

    calls: list[str] = []
    monkeypatch.setattr(
        browser_install, "prepare_at_install", lambda: calls.append("x") or {"status": "ready"}
    )
    lines: list[str] = []
    launch = installer._prepare_browser(Namespace(reuse_installed=True), lines.append)
    assert launch == {"status": "skipped", "reason": "reuse_installed"}
    assert calls == []
    install = installer._prepare_browser(Namespace(reuse_installed=False), lines.append)
    assert install["status"] == "ready" and calls == ["x"]
    assert lines[-1] == "The browser is ready."


def test_failed_browser_fetch_is_typed_and_does_not_fail_the_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.gact.browser import driver
    from clio_agent.gact.browser import install as browser_install
    from clio_agent.gact.browser.settings import BrowserError

    def offline() -> None:
        raise BrowserError("browser_runtime_install_failed", "nodejs.org is unreachable")

    monkeypatch.setattr(browser_install, "playwright_version", lambda: "1.0")
    monkeypatch.setattr(driver, "ensure_node", offline)
    assert browser_install.prepare_at_install() == {
        "status": "failed",
        "reason": "browser_runtime_install_failed",
        "error": "nodejs.org is unreachable",
    }


def test_failed_package_step_waits_for_store_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed install never exits while ``migrate`` still holds clio-core."""
    import sys

    released = Event()

    def install(workspace: Path, **kwargs: Any) -> dict[str, Any]:
        raise installer.DocumentRuntimeError("Office download failed")

    def migrate(args: Any, emit: Any) -> dict[str, Any]:
        released.wait(0.2)
        released.set()
        return {"status": "done"}

    monkeypatch.setattr(installer, "install_document_runtime", install)
    monkeypatch.setattr(installer, "_run_store_migrations", migrate)
    monkeypatch.setattr(installer, "_prepare_browser", lambda args, emit: {"status": "ready"})
    monkeypatch.setattr(
        installer, "_prepare_default_marketplace", lambda args, emit: {"status": "ready"}
    )
    monkeypatch.setattr(installer, "_prewarm_runtime_files", lambda args: {"status": "done"})
    monkeypatch.setattr(
        sys, "argv", ["document_install", "--workspace", str(tmp_path / "workspace")]
    )
    with pytest.raises(installer.DocumentRuntimeError, match="Office"):
        installer._main()
    assert released.is_set()
