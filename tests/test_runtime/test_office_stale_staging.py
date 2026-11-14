"""An Office preparation ended mid-way leaves no mounted image or partial tree for good."""

from __future__ import annotations

import plistlib
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from clio_agent.runtime.document_stack import office, office_mounts
from clio_agent.runtime.document_stack.process import DocumentError


def _abandoned(parent: Path) -> tuple[Path, Path]:
    """A staging directory as SIGKILL leaves it: a partial image and a mount point."""
    staging = parent / f"{office_mounts.STAGING_PREFIX}abandoned"
    partial = staging / "image" / "LibreOffice.app" / "Contents"
    partial.mkdir(parents=True)
    locked = partial / "Info.plist"
    locked.write_text("partial")
    locked.chmod(stat.S_IREAD)
    mount = staging / "tmpmount"
    mount.mkdir()
    return staging, mount


def _hdiutil(mounts: list[Path], seen: list[list[str]]):
    listing = plistlib.dumps(
        {
            "images": [
                {"system-entities": [{"dev-entry": "/dev/disk9"}, {"mount-point": str(m)}]}
                for m in mounts
            ]
        }
    ).decode()

    def run(command: list[str], **_kwargs: object) -> str:
        seen.append(command)
        if command[1:] == ["info", "-plist"]:
            return listing
        if command[1] == "detach":
            return ""
        return "26.2.6"

    return run


def test_another_versions_live_staging_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An update can prepare another renderer version under that version's own lock."""
    live = tmp_path / f"{office_mounts.staging_prefix('27.2.1')}abcd1234"
    mount = live / "tmpmount"
    mount.mkdir(parents=True)
    ours = tmp_path / f"{office_mounts.staging_prefix('26.2.6')}efgh5678"
    ours.mkdir()
    seen: list[list[str]] = []
    monkeypatch.setattr(office_mounts.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(office_mounts, "run", _hdiutil([mount], seen))
    office_mounts.clear_stale_staging(tmp_path, "26.2.6")
    assert live.exists()
    assert not ours.exists()
    assert [c for c in seen if c[1] == "detach"] == []


def test_a_half_detached_image_from_this_cache_is_detached_by_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / f"{office_mounts.STAGING_PREFIX}old").mkdir()
    ours = str(tmp_path / "downloads" / "LibreOffice_26.2.6.3_MacOS_aarch64.dmg")
    listing = plistlib.dumps(
        {
            "images": [
                {
                    "image-path": ours,
                    "system-entities": [
                        {"dev-entry": "/dev/disk11s1"},
                        {"dev-entry": "/dev/disk11"},
                    ],
                },
                {
                    "image-path": "/Volumes/Other.dmg",
                    "system-entities": [{"dev-entry": "/dev/disk7"}],
                },
            ]
        }
    ).decode()
    seen: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> str:
        seen.append(command)
        return listing if command[1] == "info" else ""

    monkeypatch.setattr(office_mounts.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(office_mounts, "run", run)
    office_mounts.clear_stale_staging(tmp_path, "26.2.6")
    assert [c for c in seen if c[1] == "detach"] == [
        ["/usr/bin/hdiutil", "detach", "/dev/disk11", "-force"]
    ]


def test_next_preparation_detaches_and_removes_what_a_killed_one_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "office"
    staging, mount = _abandoned(tmp_path)
    elsewhere = tmp_path / "other-volume"
    seen: list[list[str]] = []
    monkeypatch.setattr(office.platform, "system", lambda: "Darwin")
    fake = _hdiutil([mount, elsewhere], seen)
    monkeypatch.setattr(office, "run", fake)
    monkeypatch.setattr(office_mounts, "run", fake)
    monkeypatch.setattr(office, "executable", lambda _root: None)
    monkeypatch.setattr(office, "installer_prepared_tree", lambda: None)
    monkeypatch.setattr(office, "asset", lambda: (_ for _ in ()).throw(DocumentError("stop")))
    with pytest.raises(DocumentError, match="stop"):
        office.ensure_office(root)
    assert ["/usr/bin/hdiutil", "detach", str(mount), "-force"] in seen
    # Only images mounted inside abandoned staging are touched.
    assert not any(str(elsewhere) in command for command in seen)
    assert not staging.exists()


def test_without_leftovers_nothing_is_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = tmp_path / "office" / "program" / "soffice.com"
    program.parent.mkdir(parents=True)
    program.touch()
    seen: list[list[str]] = []
    monkeypatch.setattr(office.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(office.office_compat, "supported", lambda: False)
    fake = _hdiutil([], seen)
    monkeypatch.setattr(office, "run", fake)
    monkeypatch.setattr(office_mounts, "run", fake)
    assert office.ensure_office(tmp_path / "office") == str(program)
    assert [command[-1] for command in seen] == ["--version"]


@pytest.mark.parametrize("system", ["Linux", "Windows"])
def test_other_systems_remove_the_partial_tree_without_hdiutil(
    system: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging, _mount = _abandoned(tmp_path)
    seen: list[list[str]] = []
    monkeypatch.setattr(office.platform, "system", lambda: system)
    monkeypatch.setattr(office_mounts, "run", _hdiutil([], seen))
    office_mounts.clear_stale_staging(tmp_path, "26.2.6")
    assert not staging.exists()
    assert seen == []


def test_a_mount_that_will_not_detach_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    staging, mount = _abandoned(tmp_path)
    listing = _hdiutil([mount], [])

    def run(command: list[str], **kwargs: object) -> str:
        if command[1] == "detach":
            raise DocumentError("hdiutil exited 16: resource busy")
        return listing(command, **kwargs)

    monkeypatch.setattr(office.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(office_mounts, "run", run)
    monkeypatch.setattr(office_mounts.shutil, "rmtree", _refuse)
    office_mounts.clear_stale_staging(tmp_path, "26.2.6")
    assert "office_stale_mount_kept" in caplog.text
    assert "office_stale_staging_kept" in caplog.text
    assert staging.exists()


def test_an_attached_image_is_tracked_until_its_detach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "staging" / "image"
    root.mkdir(parents=True)
    seen: list[list[str]] = []
    during: list[set[str]] = []

    def copy(_source: Path, _target: Path) -> None:
        during.append(set(office_mounts.ATTACHED))
        raise KeyboardInterrupt

    monkeypatch.setattr(office.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(office, "run", lambda command, **_kwargs: seen.append(command) or "")
    monkeypatch.setattr(office, "_copy_app_bundle", copy)
    with pytest.raises(KeyboardInterrupt):
        office._extract(tmp_path / "LibreOffice.dmg", root)
    mount = seen[0][-1]
    assert during == [{mount}]
    registered: list[object] = []
    monkeypatch.setattr(office_mounts.atexit, "register", registered.append)
    monkeypatch.setattr(office_mounts.atexit, "unregister", registered.remove)
    registered.append(office_mounts._detach_attached_at_exit)
    office_mounts.track_attached(Path(mount))
    assert registered == [office_mounts._detach_attached_at_exit]
    office_mounts.ATTACHED.discard(mount)
    assert seen[-1] == ["/usr/bin/hdiutil", "detach", mount]
    assert office_mounts.ATTACHED == set()


def test_images_still_attached_at_exit_are_detached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM after SIGINT exits without the extracting worker's ``finally``."""
    mount = str(tmp_path / "tmpmount")
    seen: list[list[str]] = []
    monkeypatch.setattr(office_mounts, "ATTACHED", {mount})
    monkeypatch.setattr(office_mounts, "run", lambda command, **_kwargs: seen.append(command) or "")
    office_mounts._detach_attached_at_exit()
    assert seen == [["/usr/bin/hdiutil", "detach", mount, "-force"]]


def _refuse(path: Path, **_kwargs: object) -> None:
    raise OSError(30, "Read-only file system", str(path))


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no SIGTERM to trap")
def test_sigterm_unwinds_so_finally_cleanup_runs(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import os, signal, sys, time
        from clio_agent.runtime.document_stack.process import end_on_termination
        end_on_termination()
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(10)
        finally:
            print("detached", flush=True)
        """
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.stdout.strip() == "detached"
    assert result.returncode == 128 + 15
