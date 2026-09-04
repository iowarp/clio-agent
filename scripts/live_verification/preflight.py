#!/usr/bin/env python3
"""Preflight for the MCP-client-unification live-verification package (#1286).

Probes everything the four legs need WITHOUT running any of them: no gact
server is booted, no provider is bound, no model is ever invoked. Every check
here is either a pure filesystem/PATH probe or an in-process import of the
existing doctor-style probe functions (``runtime/mcp_launcher.py``), exactly
the class of check the campaign's own "surface/gate on reality" allowance
covers (CLAUDE.md rule 2: schema-validate / file-exists / auth-gate are fine
in core; this script does the same at the tooling layer).

Checks:
  - clio-kit launcher on PATH (+ version via ``uv tool list``)
  - the marketplace submodule + the deep-researcher pack present
  - ``runtime/mcp_launcher.py``'s own doctor probes (declared MCP launchers on
    PATH; mcp.yaml declarations parse cleanly) run directly, in-process
  - claude_code provider PRESENCE (the ``claude``/``claude.cmd`` binary on
    PATH via the same resolver ``providers/model_discovery/claude_code.py``
    uses to probe-validate models -- deliberately calling ONLY the
    presence-check half, never ``_probe_claude`` (which spawns a real
    ``claude -p`` turn); this script never invokes a model)
  - the four legs' default ports are free
  - CLIO_KIT_PATH guidance: the effective value (env or the harness default
    ``~/clio-kit``) and whether that directory exists (informational only --
    clio-kit itself works fine without it, see the DISCOVERED note below)

Verdict JSON: ``out/live-verification/preflight.json``. Exits nonzero if any
check marked ``required`` failed.

Usage::

    uv run python scripts/live_verification/preflight.py
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common as common  # noqa: E402

#: Default ports the live legs bind to (kept distinct from every existing
#: live_gate script's port: 17818/17931/17960/17970/17971).
DEFAULT_LEG_PORTS: dict[str, int] = {
    "leg_b_web_fetch": 17980,
    "leg_c_synthetic_session": 17981,
}


def _which_clio_kit() -> str | None:
    """Windows-shim-aware resolver, mirrored from ``providers/model_discovery/
    claude_code.py::_resolve_claude_binary`` (a bare ``shutil.which`` can
    return an un-executable wrapper on Windows for a uv-tool-installed CLI)."""

    if sys.platform.startswith("win"):
        found = shutil.which("clio-kit.exe") or shutil.which("clio-kit.cmd")
        if found:
            return found
    return shutil.which("clio-kit")


def _clio_kit_version() -> str:
    """Same parse ``scripts/mcp_v1_baseline.py::_clio_kit_version`` uses."""

    out = subprocess.run(["uv", "tool", "list"], capture_output=True, text=True, check=False)
    for line in out.stdout.splitlines():
        if line.startswith("clio-kit "):
            return line.strip()
    return "unknown"


def check_clio_kit() -> dict[str, Any]:
    path = _which_clio_kit()
    version = _clio_kit_version() if path else "unknown"
    ok = path is not None
    return {
        "name": "clio_kit_launcher",
        "required": True,
        "ok": ok,
        "detail": {"path": path, "version": version},
        "remediation": None
        if ok
        else "uv tool install clio-kit==2.10.6, then ensure `uv tool dir --bin` is on PATH.",
    }


def check_marketplace_submodule() -> dict[str, Any]:
    marketplace = common.REPO / "external" / "clio-agent-marketplace"
    deep_researcher = marketplace / "deep-researcher" / "AGENT.md"
    data_semantics = marketplace / "data-semantics" / "AGENT.md"
    submodule_present = marketplace.is_dir() and any(marketplace.iterdir())
    ok = submodule_present and deep_researcher.is_file()
    return {
        "name": "marketplace_deep_researcher_pack",
        "required": True,
        "ok": ok,
        "detail": {
            "marketplace_dir": str(marketplace),
            "submodule_present": submodule_present,
            "deep_researcher_agent_md": deep_researcher.is_file(),
            "data_semantics_agent_md": data_semantics.is_file(),
        },
        "remediation": None
        if ok
        else "git submodule update --init external/clio-agent-marketplace",
    }


def check_mcp_launcher_probes() -> dict[str, Any]:
    """Run ``runtime/mcp_launcher.py``'s own doctor probes in-process."""

    from clio_agent.runtime.mcp_launcher import (
        discover_declared_mcp_servers,
        probe_mcp_launchers,
        probe_mcp_yaml_declarations,
    )

    try:
        specs = discover_declared_mcp_servers()
        launcher_findings = probe_mcp_launchers(specs=specs)
        yaml_findings = probe_mcp_yaml_declarations()
        ok = not any(f.required for f in launcher_findings)
        return {
            "name": "mcp_launcher_doctor_probes",
            "required": False,  # informational: absence of a declared server here is normal
            "ok": ok,
            "detail": {
                "declared_servers": sorted(specs),
                "missing_launcher_findings": [
                    {"name": f.name, "summary": f.summary, "next_action": f.next_action}
                    for f in launcher_findings
                ],
                "unreadable_mcp_yaml_findings": [
                    {"name": f.name, "summary": f.summary} for f in yaml_findings
                ],
            },
            "remediation": None,
        }
    except Exception as exc:  # noqa: BLE001 - surfaced as a structured failure, never crash
        return {
            "name": "mcp_launcher_doctor_probes",
            "required": False,
            "ok": False,
            "detail": {"error": f"{type(exc).__name__}: {exc}"},
            "remediation": "Inspect pack/blueprint discovery; see the exception above.",
        }


def check_claude_code_presence() -> dict[str, Any]:
    """Binary-on-PATH presence ONLY -- never invokes a model (never calls
    ``_probe_claude``, which spawns a real ``claude -p`` turn)."""

    from clio_agent.providers.model_discovery.claude_code import (
        ClaudeCodeCLIUnavailableError,
        _resolve_claude_binary,
    )

    try:
        binary = _resolve_claude_binary()
        return {
            "name": "claude_code_cli_presence",
            "required": True,
            "ok": True,
            "detail": {
                "binary": binary,
                "note": "presence only; actual auth/login state is NOT checked here "
                "(that requires a live `claude -p` call, out of scope for preflight).",
            },
            "remediation": None,
        }
    except ClaudeCodeCLIUnavailableError as exc:
        return {
            "name": "claude_code_cli_presence",
            "required": True,
            "ok": False,
            "detail": {"error": str(exc)},
            "remediation": "Install Claude Code and run `claude login` once per machine.",
        }


def check_ports(ports: dict[str, int]) -> dict[str, Any]:
    findings = {name: common.port_is_free(port) for name, port in ports.items()}
    ok = all(findings.values())
    return {
        "name": "leg_ports_free",
        "required": True,
        "ok": ok,
        "detail": {"ports": ports, "free": findings},
        "remediation": None
        if ok
        else "A leg's default port is already bound; pass --port to that leg or free it.",
    }


def check_clio_kit_path() -> dict[str, Any]:
    """Informational only: CLIO_KIT_PATH is consumed by the clio-kit CLI itself
    (an external uv-tool package outside this source tree, not by
    clio_agent), so its absence is guidance, not a failure -- confirmed live:
    `clio-kit --help` works with no CLIO_KIT_PATH set and no ~/clio-kit dir."""

    import os

    effective = os.environ.get("CLIO_KIT_PATH", "") or str(Path.home() / "clio-kit")
    exists = Path(effective).expanduser().is_dir()
    return {
        "name": "clio_kit_path_guidance",
        "required": False,
        "ok": True,
        "detail": {
            "effective_value": effective,
            "source": "env:CLIO_KIT_PATH" if os.environ.get("CLIO_KIT_PATH") else "harness_default",
            "directory_exists": exists,
            "note": "clio-kit does not require this directory to exist to run "
            "(verified: `clio-kit --help`/`mcp-server` work with it absent); this is "
            "the value tests/test_real_cases/conftest.py would inject into the gact "
            "server env for the agent-test harness legs (A/D).",
        },
        "remediation": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(common.OUT_ROOT / "preflight.json"))
    args = parser.parse_args()

    checks = [
        check_clio_kit(),
        check_marketplace_submodule(),
        check_mcp_launcher_probes(),
        check_claude_code_presence(),
        check_ports(DEFAULT_LEG_PORTS),
        check_clio_kit_path(),
    ]
    required_failed = [c["name"] for c in checks if c["required"] and not c["ok"]]
    verdict = {
        "checks": checks,
        "required_failed": required_failed,
        "pass": not required_failed,
    }
    common.write_verdict(Path(args.out), verdict)
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
