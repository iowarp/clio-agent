"""MCP spawn diet (#930 S4/#934): eligibility, plan validation, learning, seam.

The diet must NEVER pick an env itself — it replays exactly what a live
declared chain was observed running, and falls back typed on any doubt.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from clio_agent.tools import spawn_diet
from clio_agent.tools.mcp_config import MCPServerSpec, transport_for


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Every test gets its own diet cache file AND fresh module state —
    the module globals otherwise leak between tests (learned keys,
    applied-plan markers) and mask real behavior."""

    monkeypatch.setattr(spawn_diet, "_cache_path", lambda: tmp_path / "diet.json")
    monkeypatch.setattr(spawn_diet, "_scans_scheduled", set())
    monkeypatch.setattr(spawn_diet, "_pending_learns", {})
    monkeypatch.setattr(spawn_diet, "_applied_plans", {})
    yield


# ---------------------------------------------------------------- eligibility


@pytest.mark.parametrize(
    ("command", "args", "eligible"),
    [
        ("clio-kit", ("mcp-server", "pandas"), True),
        ("C:/Users/x/.local/bin/clio-kit.exe", ("mcp-server", "hdf5"), True),
        ("clio-kit", ("mcp-server", "pandas", "--branch", "dev"), False),
        ("clio-kit", ("mcp-server", "--branch"), False),
        ("clio-kit", ("prompt", "pandas"), False),
        ("uvx", ("mcp-server", "pandas"), False),
        ("clio-kit", (), False),
    ],
)
def test_diet_eligibility(command, args, eligible) -> None:
    assert spawn_diet.diet_eligible(command, args) is eligible


# ------------------------------------------------------------ plan validation


def _fake_env(tmp_path: Path, name: str = "pandas") -> tuple[Path, Path]:
    """A fake materialized env: Scripts/python + Scripts/<name>-mcp entry."""

    suffix = "66677d586ca2c00fc70f8ab8"
    scripts = tmp_path / "mcp-environments" / f"{name}-{suffix}" / "Scripts"
    scripts.mkdir(parents=True)
    shim = scripts / ("python.exe" if sys.platform == "win32" else "python")
    entry = scripts / f"{name}-mcp.exe"
    shim.write_text("stub")
    entry.write_text("stub")
    return shim, entry


def _launcher(tmp_path: Path) -> str:
    launcher = tmp_path / "clio-kit"
    launcher.write_text("launcher")
    return str(launcher)


def _store_plan(
    launcher: str,
    argv: list[str],
    *,
    fingerprint: str | None = None,
    learned_at: float | None = None,
    env: dict | None = None,
) -> None:
    plans = {
        spawn_diet._plan_key(launcher, ("mcp-server", "pandas")): {
            "server": "pandas",
            "argv": argv,
            "env": env
            if env is not None
            else {"CLIO_KIT_LOCKED_SERVER_SCHEMA": "clio-kit.locked-server.v4"},
            "launcher_fingerprint": fingerprint
            if fingerprint is not None
            else spawn_diet._launcher_fingerprint(launcher),
            "learned_at": time.time() if learned_at is None else learned_at,
        }
    }
    spawn_diet._save_cache(plans)


def test_resolve_no_plan_is_a_quiet_none() -> None:
    assert spawn_diet.resolve("pandas", "clio-kit", ("mcp-server", "pandas")) is None


def test_resolve_valid_plan(tmp_path) -> None:
    shim, entry = _fake_env(tmp_path)
    launcher = _launcher(tmp_path)
    _store_plan(launcher, [str(shim), str(entry)])
    plan = spawn_diet.resolve("pandas", launcher, ("mcp-server", "pandas"))
    assert plan is not None
    assert plan["argv"][0] == str(shim)
    assert plan["env"]["CLIO_KIT_LOCKED_SERVER_SCHEMA"] == "clio-kit.locked-server.v4"


def test_resolve_rejects_changed_launcher(tmp_path) -> None:
    shim, entry = _fake_env(tmp_path)
    launcher = _launcher(tmp_path)
    _store_plan(launcher, [str(shim), str(entry)], fingerprint="999:0")
    assert spawn_diet.resolve("pandas", launcher, ("mcp-server", "pandas")) is None


def test_resolve_rejects_vanished_env(tmp_path) -> None:
    shim, entry = _fake_env(tmp_path)
    launcher = _launcher(tmp_path)
    _store_plan(launcher, [str(shim), str(entry)])
    shim.unlink()
    assert spawn_diet.resolve("pandas", launcher, ("mcp-server", "pandas")) is None


def test_resolve_rejects_malformed_plan(tmp_path) -> None:
    launcher = _launcher(tmp_path)
    _store_plan(launcher, ["only-one-element"])
    assert spawn_diet.resolve("pandas", launcher, ("mcp-server", "pandas")) is None


def test_resolve_expires_plans_past_ttl(tmp_path) -> None:
    """The staleness bound: clio-kit resolves envs from remote registry state,
    so a plan past the TTL must fall back to the declared chain and relearn."""

    shim, entry = _fake_env(tmp_path)
    launcher = _launcher(tmp_path)
    two_days_ago = time.time() - 48 * 3600
    _store_plan(launcher, [str(shim), str(entry)], learned_at=two_days_ago)
    assert spawn_diet.resolve("pandas", launcher, ("mcp-server", "pandas")) is None
    assert spawn_diet._load_cache() == {}, "expired plan must leave the cache (relearn armed)"


def test_resolve_rejects_missing_learned_at(tmp_path) -> None:
    shim, entry = _fake_env(tmp_path)
    launcher = _launcher(tmp_path)
    plans = {
        spawn_diet._plan_key(launcher, ("mcp-server", "pandas")): {
            "server": "pandas",
            "argv": [str(shim), str(entry)],
            "env": {},
            "launcher_fingerprint": spawn_diet._launcher_fingerprint(launcher),
        }
    }
    spawn_diet._save_cache(plans)
    assert spawn_diet.resolve("pandas", launcher, ("mcp-server", "pandas")) is None


def test_resolve_rejects_unprefixed_env_keys(tmp_path) -> None:
    """Apply-time enforcement: a plan env key outside the replay prefix could
    clobber workspace pinning (CLIO_KIT_ARTIFACTS) — reject the whole plan."""

    shim, entry = _fake_env(tmp_path)
    launcher = _launcher(tmp_path)
    _store_plan(
        launcher, [str(shim), str(entry)], env={"CLIO_KIT_ARTIFACTS": "C:/evil"}
    )
    assert spawn_diet.resolve("pandas", launcher, ("mcp-server", "pandas")) is None


def test_invalid_plan_rearms_relearn(tmp_path) -> None:
    """After a validation drop, the scan-dedup key is released so the next
    connect relearns instead of dead-ending until restart."""

    shim, entry = _fake_env(tmp_path)
    launcher = _launcher(tmp_path)
    key = spawn_diet._plan_key(launcher, ("mcp-server", "pandas"))
    spawn_diet._scans_scheduled.add(key)
    try:
        _store_plan(launcher, [str(shim), str(entry)], fingerprint="999:0")
        assert spawn_diet.resolve("pandas", launcher, ("mcp-server", "pandas")) is None
        assert key not in spawn_diet._scans_scheduled
        assert spawn_diet._load_cache() == {}
    finally:
        spawn_diet._scans_scheduled.discard(key)


def test_spawn_failed_drops_applied_plan(tmp_path) -> None:
    """A dieted spawn that cannot connect drops its plan — one bad plan can
    never brick a server persistently."""

    shim, entry = _fake_env(tmp_path)
    launcher = _launcher(tmp_path)
    _store_plan(launcher, [str(shim), str(entry)])
    key = spawn_diet._plan_key(launcher, ("mcp-server", "pandas"))
    spawn_diet._applied_plans["pandas"] = key
    try:
        spawn_diet.spawn_failed("pandas")
        assert spawn_diet._load_cache() == {}
        assert "pandas" not in spawn_diet._applied_plans
        # And a namespace that was never dieted is a no-op:
        spawn_diet.spawn_failed("neverdieted")
    finally:
        spawn_diet._applied_plans.pop("pandas", None)


def test_unreadable_cache_is_typed_not_fatal(tmp_path) -> None:
    spawn_diet._cache_path().write_text("{nope")
    assert spawn_diet.resolve("pandas", "clio-kit", ("mcp-server", "pandas")) is None


def test_schema_mismatch_drops_cache(tmp_path) -> None:
    spawn_diet._cache_path().write_text(
        json.dumps({"schema": "clio-agent.mcp-spawn-diet.v0", "plans": {"k": {}}})
    )
    assert spawn_diet._load_cache() == {}


def test_chain_argv_matches_windows_direct_shape() -> None:
    """The Windows trampoline shape ([clio-kit.exe, mcp-server, name]) must
    match exactly — the live chain's root argv on this platform."""

    args = ("mcp-server", "pandas")
    assert spawn_diet._chain_argv_matches(
        ["C:\\Users\\x\\.local\\bin\\CLIO-KIT.EXE", "mcp-server", "pandas"], "clio-kit.exe", args
    )
    # Variant args, extra args, wrong name: never.
    assert not spawn_diet._chain_argv_matches(
        ["C:\\x\\clio-kit.exe", "mcp-server", "pandas", "--branch", "dev"], "clio-kit.exe", args
    )
    assert not spawn_diet._chain_argv_matches(
        ["C:\\x\\clio-kit.exe", "mcp-server"], "clio-kit.exe", args
    )
    assert not spawn_diet._chain_argv_matches(
        ["C:\\x\\other.exe", "mcp-server", "pandas"], "clio-kit.exe", args
    )


def test_underscore_mount_is_refused_typed(tmp_path, monkeypatch) -> None:
    """A mount name containing '_' routes via the composite (no first-call
    hooks) — the diet must refuse it rather than run without feedback."""

    shim, entry = _fake_env(tmp_path)
    launcher = tmp_path / "clio-kit.exe"
    launcher.write_text("launcher")
    _store_plan(str(launcher), [str(shim), str(entry)])
    assert (
        spawn_diet.diet_transport_args(
            "my_pandas", str(launcher), ("mcp-server", "pandas"), {}
        )
        is None
    )


# ----------------------------------------------------------------- derivation


def test_derive_diet_argv_swaps_interpreter(tmp_path) -> None:
    shim, entry = _fake_env(tmp_path)
    derived = spawn_diet._derive_diet_argv(["C:/base/python.exe", str(entry), "--flag"])
    assert derived == [str(shim), str(entry), "--flag"]


def test_derive_diet_argv_requires_shim(tmp_path) -> None:
    shim, entry = _fake_env(tmp_path)
    shim.unlink()
    assert spawn_diet._derive_diet_argv(["C:/base/python.exe", str(entry)]) is None
    assert spawn_diet._derive_diet_argv(["just-python"]) is None


# -------------------------------------------------------------------- learning


LEAF = textwrap.dedent(
    """
    import sys, time
    time.sleep(60)
    """
)

# A fake clio-kit launcher script: spawns the leaf (entry path via env) and
# idles, exactly like the real trampoline chain. Run via the shebang shape
# ([python, <clio-kit>, mcp-server, <name>]) which the matcher must accept —
# this doubles as coverage for the POSIX uv-tool launcher (a shebang script).
FAKE_CLIO_KIT = textwrap.dedent(
    """
    import os, subprocess, sys, time
    leaf = subprocess.Popen([sys.executable, os.environ["FAKE_LEAF_ENTRY"]])
    time.sleep(60)
    """
)


def _fake_chain(tmp_path: Path, env_dir: str, sha: str):
    """Spawn a live fake chain; returns (proc, launcher_path, entry_path, shim)."""

    scripts = tmp_path / "mcp-environments" / env_dir / "Scripts"
    scripts.mkdir(parents=True)
    shim = scripts / ("python.exe" if sys.platform == "win32" else "python")
    shim.write_text("stub")
    entry = scripts / "pandas-mcp.py"
    entry.write_text(LEAF)
    launcher = tmp_path / "clio-kit"
    launcher.write_text(FAKE_CLIO_KIT)
    env = {
        **os.environ,
        "FAKE_LEAF_ENTRY": str(entry),
        "CLIO_KIT_LOCKED_SERVER_PROJECT_SHA256": sha,
        "CLIO_KIT_LOCKED_SERVER_SCHEMA": "clio-kit.locked-server.v4",
    }
    proc = subprocess.Popen(
        [sys.executable, str(launcher), "mcp-server", "pandas"], env=env
    )
    return proc, launcher, entry, shim


def _scan_until(spec_name, launcher, args, want: str, timeout: float = 20.0) -> str:
    """Scan repeatedly until the wanted status stabilizes (chains materialize
    through intermediate wrappers, so early passes legitimately differ)."""

    deadline = time.time() + timeout
    status = "chain_not_found"
    while time.time() < deadline:
        status = spawn_diet._learn_scan(spec_name, str(launcher), args)
        if status == want:
            return status
        time.sleep(0.5)
    return status


def test_learn_scan_captures_live_chain(tmp_path) -> None:
    """A real chain (fake clio-kit launcher script -> python leaf) in a valid
    env layout with the launcher's lock env stamped."""

    suffix = "66677d586ca2c00fc70f8ab8"
    sha = suffix + "cf56d98916c054cc5ac6a65703e7a1967132bee3"
    proc, launcher, entry, shim = _fake_chain(tmp_path, f"pandas-{suffix}", sha)
    try:
        status = _scan_until("pandas", launcher, ("mcp-server", "pandas"), "learned")
        assert status == "learned", f"learn scan ended {status!r}"
    finally:
        proc.kill()

    plans = spawn_diet._load_cache()
    (plan,) = plans.values()
    assert plan["argv"][0] == str(shim)
    assert plan["argv"][1] == str(entry)
    assert plan["env"]["CLIO_KIT_LOCKED_SERVER_PROJECT_SHA256"] == sha
    # The load-bearing filter claim: the leaf inherited the FULL os.environ,
    # so anything unprefixed in the plan means the filter is broken.
    assert plan["env"] and all(
        k.startswith("CLIO_KIT_LOCKED_SERVER_") for k in plan["env"]
    ), f"unprefixed env leaked into the plan: {sorted(plan['env'])}"
    assert isinstance(plan["learned_at"], float)


def test_learn_scan_refuses_sha_mismatch(tmp_path) -> None:
    """A chain whose env dir hash does not prefix the stamped sha is refused."""

    proc, launcher, _entry, _shim = _fake_chain(
        tmp_path, "pandas-deadbeef", "66677d586ca2c00fc70f8ab8ffff"
    )
    try:
        status = _scan_until("pandas", launcher, ("mcp-server", "pandas"), "layout_mismatch")
        assert status == "layout_mismatch"
    finally:
        proc.kill()
    assert spawn_diet._load_cache() == {}


def test_learn_scan_refuses_foreign_env_dir(tmp_path) -> None:
    """An env dir that is not THIS server's (wrong name prefix) is refused even
    with a consistent sha — the largest-RSS walk may have landed on a
    subprocess the server itself spawned."""

    suffix = "66677d586ca2c00fc70f8ab8"
    sha = suffix + "cf56d98916c054cc5ac6a65703e7a1967132bee3"
    proc, launcher, _entry, _shim = _fake_chain(tmp_path, f"othersrv-{suffix}", sha)
    try:
        status = _scan_until("pandas", launcher, ("mcp-server", "pandas"), "layout_mismatch")
        assert status == "layout_mismatch"
    finally:
        proc.kill()
    assert spawn_diet._load_cache() == {}


def test_learn_scan_never_matches_a_variant_invocation(tmp_path) -> None:
    """A ``--branch dev`` chain must NOT be learned for the vanilla key — a
    prefix match here would persist the variant's env for vanilla spawns."""

    suffix = "66677d586ca2c00fc70f8ab8"
    sha = suffix + "cf56d98916c054cc5ac6a65703e7a1967132bee3"
    scripts = tmp_path / "mcp-environments" / f"pandas-{suffix}" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / ("python.exe" if sys.platform == "win32" else "python")).write_text("stub")
    entry = scripts / "pandas-mcp.py"
    entry.write_text(LEAF)
    launcher = tmp_path / "clio-kit"
    launcher.write_text(FAKE_CLIO_KIT)
    env = {**os.environ, "FAKE_LEAF_ENTRY": str(entry), "CLIO_KIT_LOCKED_SERVER_PROJECT_SHA256": sha}
    proc = subprocess.Popen(
        [sys.executable, str(launcher), "mcp-server", "pandas", "--branch", "dev"], env=env
    )
    try:
        # Give the variant chain ample time to be up, then scan for VANILLA.
        deadline = time.time() + 10
        while time.time() < deadline:
            import psutil

            if psutil.Process(proc.pid).children(recursive=True):
                break
            time.sleep(0.5)
        status = spawn_diet._learn_scan("pandas", str(launcher), ("mcp-server", "pandas"))
        assert status == "chain_not_found"
    finally:
        proc.kill()
    assert spawn_diet._load_cache() == {}


# ----------------------------------------------------------------------- seam


def test_transport_for_applies_valid_plan(tmp_path, monkeypatch) -> None:
    shim, entry = _fake_env(tmp_path)
    launcher = tmp_path / "clio-kit.exe"
    launcher.write_text("launcher")
    monkeypatch.setattr("shutil.which", lambda cmd: str(launcher))
    _store_plan(str(launcher), [str(shim), str(entry)])

    spec = MCPServerSpec(
        name="pandas", transport="stdio", command="clio-kit", args=("mcp-server", "pandas")
    )
    transport = transport_for(spec, cwd=str(tmp_path))
    assert transport.command == str(shim)
    assert transport.args == [str(entry)]
    assert transport.env["CLIO_KIT_LOCKED_SERVER_SCHEMA"] == "clio-kit.locked-server.v4"
    assert transport.env["CLIO_KIT_ARTIFACTS"] == str(tmp_path), "workspace pinning survives"


def test_transport_for_ineligible_spec_untouched(tmp_path, monkeypatch) -> None:
    launcher = tmp_path / "othertool.exe"
    launcher.write_text("launcher")
    monkeypatch.setattr("shutil.which", lambda cmd: str(launcher))
    spec = MCPServerSpec(
        name="other", transport="stdio", command="othertool", args=("serve",)
    )
    transport = transport_for(spec, cwd=str(tmp_path))
    assert transport.command == str(launcher)
    assert transport.args == ["serve"]


def test_kill_switch_is_typed_and_disables(tmp_path, monkeypatch) -> None:
    shim, entry = _fake_env(tmp_path)
    launcher = tmp_path / "clio-kit.exe"
    launcher.write_text("launcher")
    monkeypatch.setattr("shutil.which", lambda cmd: str(launcher))
    _store_plan(str(launcher), [str(shim), str(entry)])
    monkeypatch.setenv("CLIO_MCP_SPAWN_DIET", "0")
    spec = MCPServerSpec(
        name="pandas", transport="stdio", command="clio-kit", args=("mcp-server", "pandas")
    )
    transport = transport_for(spec, cwd=str(tmp_path))
    assert transport.command == str(launcher), "kill switch must force the declared command"


def test_transport_for_no_plan_registers_learn_on_connect(tmp_path, monkeypatch) -> None:
    """No plan yet: the declared command spawns and the namespace registers as
    learn-on-connect (the scan must NOT start at build time — under lazy
    routing (#932) the chain does not exist until the first tool call)."""

    launcher = tmp_path / "clio-kit.exe"
    launcher.write_text("launcher")
    monkeypatch.setattr("shutil.which", lambda cmd: str(launcher))
    monkeypatch.setattr(spawn_diet, "_pending_learns", {})
    scheduled: list[str] = []
    monkeypatch.setattr(
        spawn_diet, "schedule_learn", lambda name, cmd, args: scheduled.append(name)
    )
    spec = MCPServerSpec(
        name="pandas", transport="stdio", command="clio-kit", args=("mcp-server", "pandas")
    )
    transport = transport_for(spec, cwd=str(tmp_path))
    assert transport.command == str(launcher)
    assert scheduled == [], "the scan must wait for the connect hook"
    assert "pandas" in spawn_diet._pending_learns

    # The executor's connect hook fires the scan for the registered namespace.
    spawn_diet.namespace_connected("pandas")
    assert scheduled == ["pandas"]
    # ... and an unregistered namespace is a no-op.
    spawn_diet.namespace_connected("neverheardof")
    assert scheduled == ["pandas"]


def test_dieted_connect_success_does_not_rescan(tmp_path, monkeypatch) -> None:
    """After a DIETED spawn connects, there is no declared chain to learn from
    — the connect hook must not burn scans on it."""

    monkeypatch.setattr(spawn_diet, "_pending_learns", {"pandas": ("pandas", "c", ())})
    monkeypatch.setattr(spawn_diet, "_applied_plans", {"pandas": "some-key"})
    scheduled: list[str] = []
    monkeypatch.setattr(
        spawn_diet, "schedule_learn", lambda name, cmd, args: scheduled.append(name)
    )
    spawn_diet.namespace_connected("pandas")
    assert scheduled == []
    assert "pandas" not in spawn_diet._applied_plans
