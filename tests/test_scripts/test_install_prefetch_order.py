"""install.sh gives LibreOffice the line first on the one-liner (gact-tui#739).

The one-liner's prefetches share one saturated line, so they all land together and
Office, with the longest tail after it lands (mount, copy, a first conversion), decides
when the install ends. ``start_prefetch PLAN office-first`` starts the office entries
first and holds a large file of another kind (Chromium) until LibreOffice has landed or
``PREFETCH_WAIT_S`` has passed, behind an empty ``NAME.part`` so the app waits for it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.test_scripts.installer_shims import ROOT

pytestmark = pytest.mark.skipif(os.name == "nt", reason="needs bash")

INSTALL = ROOT / "install" / "install.sh"
_SHA = "a" * 64
_MB = 1024 * 1024
_PLAN = (
    f"browser {_SHA} {190 * _MB} chrome.zip https://example.invalid/chrome.zip\n"
    f"npm {_SHA} {10 * _MB} pnpm.tgz https://example.invalid/pnpm.tgz\n"
    f"office {_SHA} {290 * _MB} LibreOffice.dmg https://example.invalid/lo.dmg\n"
    "capability installer-downloads\n"
)


def _functions() -> str:
    text = INSTALL.read_text()
    start = text.index("start_job() {")
    end = text.index("\n}\n", text.index("start_prefetch() {")) + 3
    return text[start:end]


def _run(tmp_path: Path, mode: str, office_s: float, wait_s: int = 40) -> tuple[list[str], str]:
    scratch = tmp_path / "scratch"
    (scratch / "downloads").mkdir(parents=True)
    plan = tmp_path / "plan"
    plan.write_text(_PLAN)
    log = tmp_path / "order.log"
    script = (
        f"{_functions()}\n"
        f"PREFETCH_WAIT_S={wait_s}\n"
        'say() { echo "say $*" >> "$LOG"; }\n'
        "prefetch() {\n"
        '  echo "start $2" >> "$LOG"\n'
        f'  [ "$2" != LibreOffice.dmg ] || sleep {office_s}\n'
        '  echo "end $2" >> "$LOG"\n'
        "}\n"
        # The parent shell logs each launch, in order, before the job forks: the
        # background jobs' own lines race each other and say nothing about order.
        "start_job() {\n"
        '  case "$1" in\n'
        '    prefetch) echo "launch now $3" ;;\n'
        '    prefetch_after) [ -e "$DESKTOP_SCRATCH/downloads/$4.part" ] && p=" placeholder" || p=""\n'
        '      echo "launch held $4$p" ;;\n'
        '  esac >> "$LOG"\n'
        '  "$@" & DESKTOP_JOBS="$DESKTOP_JOBS $!"\n'
        "}\n"
        'DESKTOP_JOBS=""; DESKTOP_PLAN_BYTES=0\n'
        f'start_prefetch "$PLAN" {mode}\n'
        "wait $DESKTOP_JOBS\n"
        'echo "bytes $DESKTOP_PLAN_BYTES" >> "$LOG"\n'
    )
    env = {"PATH": os.environ["PATH"], "LOG": str(log), "PLAN": str(plan)}
    env["DESKTOP_SCRATCH"] = str(scratch)
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return log.read_text().splitlines(), result.stderr


def test_office_first_holds_chromium_until_libreoffice_lands(tmp_path: Path) -> None:
    lines, _ = _run(tmp_path, "office-first", office_s=1.0)
    launches = [line for line in lines if line.startswith("launch ")]
    assert launches[0] == "launch now LibreOffice.dmg"
    assert "launch held chrome.zip placeholder" in launches
    assert "launch now pnpm.tgz" in launches
    assert lines.index("end LibreOffice.dmg") < lines.index("start chrome.zip")
    assert f"bytes {490 * _MB}" in lines
    assert "say Fetching at the same time: LibreOffice.dmg, chrome.zip, pnpm.tgz" in lines


def test_office_first_wait_is_capped(tmp_path: Path) -> None:
    lines, _ = _run(tmp_path, "office-first", office_s=4.0, wait_s=1)
    assert lines.index("start chrome.zip") < lines.index("end LibreOffice.dmg")


def test_without_office_first_everything_starts_at_once(tmp_path: Path) -> None:
    lines, _ = _run(tmp_path, "", office_s=1.0)
    launches = sorted(line for line in lines if line.startswith("launch "))
    assert launches == [
        f"launch now {name}" for name in ("LibreOffice.dmg", "chrome.zip", "pnpm.tgz")
    ]
    assert not (tmp_path / "scratch" / "downloads" / "chrome.zip.part").exists()


def test_plan_entries_without_kind_keeps_four_fields(tmp_path: Path) -> None:
    plan = tmp_path / "plan"
    plan.write_text(_PLAN)
    script = f"{_functions()}\nplan_entries {plan}; echo --; plan_entries {plan} kind\n"
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    plain, kinded = out.stdout.split("--\n")
    assert [len(line.split()) for line in plain.splitlines()] == [4, 4, 4]
    assert [line.split()[0] for line in kinded.splitlines()] == ["browser", "npm", "office"]
