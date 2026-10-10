"""Desktop boots reuse checked root ACLs without rescanning whole environments."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clio_agent.runtime import codex_desktop_access as access
from clio_agent.runtime import sandbox_cli
from clio_agent.tools import desktop_mcp_runtime


def test_bundled_plan_uses_only_its_runtime_cache_and_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desktop_mcp_runtime, "resolve_bundled_runtime_root", lambda: tmp_path)
    monkeypatch.setattr(access.paths, "user_cache_dir", lambda: tmp_path / "private-cache")
    monkeypatch.delenv("CLIO_KIT_CACHE_DIR", raising=False)
    plan = sandbox_cli._resolve_fleet_runtime_paths()
    assert plan[:2] == [
        ("bundled_runtime", str(tmp_path), True),
        ("clio_kit_cache", str(tmp_path / "private-cache" / "mcp-runtime"), True),
    ]
    assert plan[2][0] == "user_temp" and plan[2][2] is False
    assert not any("uv_" in label for label, _, _ in plan)


@pytest.mark.parametrize("change", ["none", "acl", "directory", "corrupt_receipt", "failed_grant"])
def test_access_reuses_only_successful_unchanged_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    config = tmp_path / "config"
    monkeypatch.setattr(access.paths, "user_config_dir", lambda: config)
    grant = sandbox_cli.FleetGrant("bundled", str(root), ("offline", "online"), True, True)
    monkeypatch.setattr(access, "build_fleet_runtime_grant_plan", lambda: [grant])
    acl = ["original"]
    queries: list[list[str]] = []
    applied: list[object] = []

    def query(argv: list[str]) -> tuple[int, str]:
        queries.append(argv)
        return 0, acl[0]

    def apply(**kwargs: object) -> list[dict[str, object]]:
        applied.append(kwargs)
        return [
            {
                "grant": "bundled",
                "user": user,
                "status": "failed" if change == "failed_grant" else "granted",
            }
            for user in grant.users
        ]

    monkeypatch.setattr(access, "_run_icacls", query)
    monkeypatch.setattr(access, "grant_fleet_runtime_access", apply)
    stages: list[str] = []
    access.ensure_desktop_runtime_access(progress=stages.append)
    if change == "acl":
        acl[0] = "permissions changed"
    elif change == "directory":
        root.rename(tmp_path / "previous-runtime")
        root.mkdir()
    elif change == "corrupt_receipt":
        (config / "sandbox" / "desktop-runtime-access.json").write_text("[]")
    result = access.ensure_desktop_runtime_access(progress=stages.append)
    assert len(applied) == (1 if change == "none" else 2)
    assert all(len(argv) == 2 for argv in queries)  # ACL reads never walk children.
    if change == "none":
        assert all(item.get("cached") is True for item in result)
    saved = json.loads((config / "sandbox" / "desktop-runtime-access.json").read_text())
    assert bool(saved["grants"]) is (change != "failed_grant")


def test_unreadable_acl_is_never_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access.paths, "user_config_dir", lambda: tmp_path / "config")
    grant = sandbox_cli.FleetGrant("runtime", str(tmp_path), ("offline",), True, True)
    monkeypatch.setattr(access, "build_fleet_runtime_grant_plan", lambda: [grant])
    monkeypatch.setattr(access, "_run_icacls", lambda argv: (1, "access denied"))
    calls: list[object] = []

    def apply(**kwargs: object) -> list[dict[str, object]]:
        calls.append(kwargs)
        return [{"status": "granted"}]

    monkeypatch.setattr(access, "grant_fleet_runtime_access", apply)
    access.ensure_desktop_runtime_access()
    access.ensure_desktop_runtime_access()
    assert len(calls) == 2


def test_tool_cache_is_created_before_granting_future_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "tool-cache"
    monkeypatch.setattr(access.paths, "user_config_dir", lambda: tmp_path / "config")
    grant = sandbox_cli.FleetGrant("clio_kit_cache", str(cache), ("offline",), True, False)
    monkeypatch.setattr(access, "build_fleet_runtime_grant_plan", lambda: [grant])
    monkeypatch.setattr(access, "_run_icacls", lambda argv: (0, "root permissions"))

    def apply(**kwargs: Any) -> list[dict[str, object]]:
        assert cache.is_dir()
        assert kwargs["plan"][0].exists
        assert kwargs["combine_users"] is True
        return [{"status": "granted"}]

    monkeypatch.setattr(access, "grant_fleet_runtime_access", apply)
    access.ensure_desktop_runtime_access()


@pytest.mark.parametrize("inherit", [True, False])
@pytest.mark.parametrize("code", [0, 5])
def test_combined_grant_visits_once_and_never_certifies_partial_success(
    tmp_path: Path, inherit: bool, code: int
) -> None:
    grant = sandbox_cli.FleetGrant("runtime", str(tmp_path), ("offline", "online"), inherit, True)
    calls: list[list[str]] = []

    def run(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return code, "a child denied access" if code else "completed"

    records = sandbox_cli.grant_fleet_runtime_access(
        runner=run, plan=[grant], platform="win32", combine_users=True
    )
    permission = "(OI)(CI)(RX)" if inherit else "(RX)"
    assert calls == [
        [
            "icacls",
            str(tmp_path),
            "/grant",
            f"offline:{permission}",
            f"online:{permission}",
            *(["/T"] if inherit else []),
            "/Q",
        ]
    ]
    assert [record["user"] for record in records] == ["offline", "online"]
    assert all(record["status"] == ("granted" if code == 0 else "failed") for record in records)
    if code:
        assert all(record["detail"] == "a child denied access" for record in records)
