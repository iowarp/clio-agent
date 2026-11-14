"""The parallel inherited grant matches ``icacls`` and falls back to it on any error."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from clio_agent.runtime import acl_parallel
from clio_agent.runtime import sandbox_cli as scli


def _grant(inherit: bool = True) -> scli.FleetGrant:
    return scli.FleetGrant("bundled_runtime", "C:\\runtime", ("offline", "online"), inherit, True)


def test_parallel_grant_replaces_icacls_when_it_succeeds() -> None:
    applied: list[tuple[str, tuple[str, ...]]] = []
    ran: list[list[str]] = []
    records = scli.grant_fleet_runtime_access(
        runner=lambda argv: ran.append(argv) or (0, ""),
        plan=[_grant()],
        platform="win32",
        combine_users=True,
        parallel=lambda path, users: applied.append((path, tuple(users))) or {},
    )
    assert applied == [("C:\\runtime", ("offline", "online"))]
    assert ran == []
    assert [(r["user"], r["status"], r["rc"]) for r in records] == [
        ("offline", "granted", 0),
        ("online", "granted", 0),
    ]


@pytest.mark.parametrize("code", [0, 5])
def test_parallel_failure_falls_back_to_icacls(code: int) -> None:
    ran: list[list[str]] = []

    def broken(path: str, users: object) -> None:
        raise OSError("ACL write failed")

    records = scli.grant_fleet_runtime_access(
        runner=lambda argv: ran.append(argv) or (code, "denied" if code else ""),
        plan=[_grant()],
        platform="win32",
        combine_users=True,
        parallel=broken,
    )
    assert ran == _grant().icacls_argv(combine_users=True)
    assert all(r["status"] == ("failed" if code else "granted") for r in records)


def test_traverse_and_per_user_grants_never_use_the_parallel_grant() -> None:
    def unexpected(path: str, users: object) -> None:
        raise AssertionError("parallel grant used")

    for grant, combine in ((_grant(inherit=False), True), (_grant(), False)):
        ran: list[list[str]] = []
        scli.grant_fleet_runtime_access(
            runner=lambda argv, ran=ran: ran.append(argv) or (0, ""),
            plan=[grant],
            platform="win32",
            combine_users=combine,
            parallel=unexpected,
        )
        assert ran == grant.icacls_argv(combine_users=combine)


def _tree(root: Path) -> None:
    for pkg in range(6):
        for sub in range(3):
            leaf = root / "python" / "site-packages" / f"pkg{pkg}" / f"sub{sub}"
            leaf.mkdir(parents=True)
            for name in range(10):
                (leaf / f"m{name}.py").write_text("x")
    (root / "bin").mkdir()
    (root / "bin" / "tool.exe").write_text("x")
    (root / "python" / "python.exe").write_text("x")
    (root / "root.txt").write_text("x")


def test_split_covers_every_object_once_with_parents_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runtime"
    _tree(root)
    monkeypatch.setattr(acl_parallel, "MIN_SUBTREE", 8)
    levels, frontier = acl_parallel.plan_split(str(root), workers=2)
    intermediates = [path for level in levels for path, _parent in level]
    assert {p[len(str(root)) + 1 :] for p in intermediates} == {
        "python",
        str(Path("python/site-packages")),
        *(str(Path(f"python/site-packages/pkg{n}")) for n in range(6)),
    }
    seen = {str(root)}
    for level in levels:
        assert all(parent in seen for _path, parent in level)
        seen.update(path for path, _parent in level)
    # Frontier items sit directly under the root or an intermediate, and each
    # object below them is reached by exactly one frontier propagation.
    assert all(str(Path(item).parent) in seen for item in frontier)
    covered = set(intermediates) | set(frontier)
    for item in frontier:
        if Path(item).is_dir():
            covered.update(str(p) for p in Path(item).rglob("*"))
    everything = {str(p) for p in root.rglob("*")}
    assert covered == everything
    assert len(intermediates) + len(frontier) == len(set(intermediates) | set(frontier))


def test_split_refuses_reparse_points(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    if sys.platform == "win32":
        import _winapi  # noqa: PLC0415

        _winapi.CreateJunction(str(tmp_path), str(root / "link"))
    else:
        (root / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(acl_parallel.ParallelGrantUnavailable):
        acl_parallel.plan_split(str(root), workers=2)


_USERS = ["BUILTIN\\Performance Log Users", "BUILTIN\\Performance Monitor Users"]


def _icacls(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["icacls", *args], capture_output=True, text=True, check=False)


def _saved(root: Path, out: Path) -> list[str]:
    assert _icacls(str(root), "/save", str(out), "/T", "/C", "/Q").returncode == 0
    lines = out.read_text(encoding="utf-16-le").splitlines()
    return [ln.replace(root.name, "ROOT", 1) if i % 2 == 0 else ln for i, ln in enumerate(lines)]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
@pytest.mark.parametrize("twice", [False, True])
def test_parallel_grant_writes_exactly_what_icacls_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, twice: bool
) -> None:
    monkeypatch.setattr(acl_parallel, "MIN_SUBTREE", 8)
    serial, parallel = tmp_path / "serial", tmp_path / "parallel"
    for root in (serial, parallel):
        _tree(root)
        # A protected directory, explicit ACEs above and below the split.
        for rel, args in (
            ("bin", ["/inheritance:d"]),
            ("python/site-packages/pkg1/sub0", ["/inheritance:r", "/grant", "*S-1-5-32-545:(F)"]),
            ("python/site-packages/pkg2", ["/grant", "*S-1-5-32-545:(OI)(CI)(M)"]),
            ("python", ["/grant", "*S-1-5-32-545:(CI)(W)"]),
        ):
            assert _icacls(str(root / rel), *args, "/Q").returncode == 0
    for _ in range(2 if twice else 1):
        grant = _icacls(str(serial), "/grant", *(f"{u}:(OI)(CI)(RX)" for u in _USERS), "/Q")
        if grant.returncode:
            pytest.skip(f"built-in group names are localized here: {grant.stdout}")
        stats = acl_parallel.grant_inherited_read_execute(str(parallel), _USERS, workers=4)
        assert stats["intermediates"] > 0
    assert _saved(parallel, tmp_path / "p.txt") == _saved(serial, tmp_path / "s.txt")
