"""Pin the bundled-runtime ARC smoke against what broke the v0.9.4.1 build.

``install/build-gact-runtime.ps1`` proves the RELOCATED runtime image can
initialize clio-core rather than degrading to LocalFS (the silent-until-shipped
casualty of an over-eager prune). That gate is new in v0.9.4.1 and it failed the
Windows bundled desktop build twice, for two different reasons, and both times
the reason was invisible: the check was piped to ``Out-Null``, so CI reported a
bare ``exit 1``.

What is pinned here is that the failure can always be read: the smoke runs
through :mod:`install.arc_smoke`, which on a degrade re-runs the initialization
without ARC's loud-degrade wrapper to recover the traceback and dumps the
daemon log. The environment pins keep the smoke from failing for reasons that
say nothing about the image, without weakening what it asserts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "install" / "build-gact-runtime.ps1"
HELPER = REPO_ROOT / "install" / "arc_smoke.py"


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _arc_smoke_block(script: str) -> str:
    """Return the ARC smoke section: its setup through the ``finally`` restore."""
    start = script.index("$previousUserDir = $env:CLIO_USER_DIR")
    end = script.index("sanity (relocated boot)")
    return script[start:end]


def test_arc_smoke_requests_a_small_file_tier(script: str) -> None:
    """The smoke proves CTE initializes; it must not demand a 1 GiB tier.

    ARC requires ``capacity + 1 GiB reserve`` free on the tier's filesystem, so
    a 1 GiB request needs >2 GiB free on a volume the build has just filled.
    """
    block = _arc_smoke_block(script)
    capacity = re.search(r"CLIO_ARC_CTE_FILE_CAPACITY\s*=\s*'([^']+)'", block)
    assert capacity is not None, "the ARC smoke no longer sets a file capacity"
    value = capacity.group(1)
    match = re.fullmatch(r"(\d+)MB", value)
    assert match is not None, f"expected a modest MB-scale smoke tier, found {value!r}"
    assert int(match.group(1)) <= 256


def test_arc_smoke_user_dir_is_off_the_relocated_copy(script: str) -> None:
    """The smoke's user state must not land inside the relocated temp copy.

    The relocated tree lives on the temp volume next to a second ~1 GB copy of
    the runtime. Where the user dir lives has no bearing on what is proven.
    """
    block = _arc_smoke_block(script)
    assert "Join-Path $reloc 'smoke-user'" not in block
    assert "$smokeUser = (Join-Path ([System.IO.Path]::GetDirectoryName($Out))" in block


def test_arc_smoke_is_hermetic(script: str) -> None:
    """The runtime state dir is pinned, so the smoke never shares ``~/.clio``.

    The spawn lock, pidfile, client registry and daemon log default to a
    host-global directory. Pinning them to the smoke's own directory is what
    makes this prove the relocated image, and is why the failure path can find
    the daemon log at all.
    """
    block = _arc_smoke_block(script)
    assert "$env:CLIO_RUNTIME_STATE_DIR = (Join-Path $smokeUser 'runtime-state')" in block
    assert "Remove-Item Env:CLIO_RUNTIME_STATE_DIR" in block, "the smoke leaks its state dir"


def test_arc_smoke_surfaces_its_failure(script: str) -> None:
    """The smoke's output must reach the build log, not ``Out-Null``."""
    block = _arc_smoke_block(script)
    invoke = block.index("install/arc_smoke.py")
    assert "Out-Null" not in block[invoke : invoke + 200], (
        "the ARC smoke is swallowing its diagnostic output again"
    )


def test_arc_smoke_cleans_up_after_itself(script: str) -> None:
    """The smoke directory sits beside the bundle, so it must be removed."""
    block = _arc_smoke_block(script)
    assert block.count("Remove-Item -LiteralPath $smokeUser -Recurse -Force") >= 2


def test_helper_recovers_the_traceback_and_the_daemon_log() -> None:
    """A degrade must yield the stack ARC's loud-degrade wrapper swallowed.

    ``make_arc_store`` reports a typed reason and returns LocalFSStore, so the
    exception never reaches the caller; re-running the init without it is the
    only way the build log gets a stack to act on.
    """
    helper = HELPER.read_text(encoding="utf-8")
    assert "traceback.print_exc()" in helper
    assert "preflight_clio_core_config" in helper
    assert "ClioCoreStore(config_path=config_path)" in helper
    assert "clio-runtime.log" in helper
    assert "runtime_state_dir" in helper, "the log dump must follow the pinned state dir"
