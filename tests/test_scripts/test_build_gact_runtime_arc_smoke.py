"""Pin the bundled-runtime ARC smoke against the failure that broke v0.9.4.1.

``install/build-gact-runtime.ps1`` proves the RELOCATED runtime image can
initialize clio-core rather than degrading to LocalFS (the silent-until-shipped
casualty of an over-eager prune). On the v0.9.4.1 tag that gate failed the
Windows bundled desktop build, and the reason was invisible: the smoke asked
for a 1 GiB CTE file tier inside the temp volume the build had just filled with
two ~1 GB copies of the runtime, ARC's preflight (capacity + a 1 GiB
free-space reserve) could not fit it, and the resulting LOUD degrade was piped
to ``Out-Null`` -- so the build reported only ``exit 1``.

These are text assertions because the subject is a PowerShell script; they pin
the three properties that made the difference, none of which weaken what the
gate actually proves.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "install" / "build-gact-runtime.ps1"


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
    the runtime; putting the CTE tier there is what ran it out of space. Where
    the user dir lives has no bearing on what the smoke proves.
    """
    block = _arc_smoke_block(script)
    assert "Join-Path $reloc 'smoke-user'" not in block
    assert "$smokeUser = (Join-Path ([System.IO.Path]::GetDirectoryName($Out))" in block


def test_arc_smoke_surfaces_its_failure(script: str) -> None:
    """A degrade's typed reason must reach the build log, not ``Out-Null``.

    The gate's whole value is naming WHY clio-core was unavailable; swallowing
    stderr turns it into a bare exit code and costs a release cycle to diagnose.
    """
    block = _arc_smoke_block(script)
    store_call = block.index("make_arc_store(backend=")
    tail = block[store_call : store_call + 400]
    assert "Out-Null" not in tail, "the ARC smoke is swallowing its degrade reason again"
    assert "clio-runtime.log" in block, "the failure path no longer dumps the runtime log"


def test_arc_smoke_cleans_up_after_itself(script: str) -> None:
    """The smoke directory sits beside the bundle, so it must be removed."""
    block = _arc_smoke_block(script)
    assert block.count("Remove-Item -LiteralPath $smokeUser -Recurse -Force") >= 2
