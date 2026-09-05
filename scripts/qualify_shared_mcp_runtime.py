"""Qualify one persistent CLIO Kit science installation against four MCP servers.

This script deliberately launches the installed ``clio-kit`` executable for every
server. It never uses ``uvx`` and never asks CLIO Kit to enter isolated mode.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

SERVERS = ("ndp", "geo", "pandas", "plot")
EXPECTED_TOOLS = {
    "ndp": "search_datasets",
    "geo": "geocode",
    "pandas": "profile_csv",
    "plot": "line_plot",
}
OWNERSHIP_MARKER = "clio-agent.shared-mcp-qualification.v1"
RUNTIME_SCHEMA = "clio-kit.shared-runtime.v1"


def science_package_spec(package: str) -> str:
    """Return a package or local-wheel spec selecting the science union."""
    value = package.strip()
    if not value:
        raise ValueError("the CLIO Kit package spec cannot be empty")
    if "[" in value:
        raise ValueError("pass the base package or wheel; the script adds [science]")
    versioned = re.fullmatch(r"([A-Za-z0-9_.-]+)([<>=!~].+)", value)
    if versioned:
        return f"{versioned.group(1)}[science]{versioned.group(2)}"
    return f"{value}[science]"


def prepare_runtime_root(root: Path) -> tuple[Path, dict[str, str]]:
    """Create a fresh owned root and return a fully contained environment."""
    root = root.resolve()
    if root.exists():
        raise RuntimeError(
            f"refusing to reuse qualification root (it may contain a live runtime): {root}"
        )
    root.mkdir(parents=True)
    (root / "owner.txt").write_text(OWNERSHIP_MARKER, encoding="utf-8")
    environment = os.environ.copy()
    groups = {
        "temp": ("TEMP", "TMP", "TMPDIR"),
        "uv-cache": ("UV_CACHE_DIR",),
        "uv-python": ("UV_PYTHON_INSTALL_DIR",),
        "tools": ("UV_TOOL_DIR",),
        "bin": ("UV_TOOL_BIN_DIR",),
        "pip-cache": ("PIP_CACHE_DIR",),
        "bytecode": ("PYTHONPYCACHEPREFIX",),
        "cache": ("XDG_CACHE_HOME",),
        "data": ("XDG_DATA_HOME",),
        "config": ("XDG_CONFIG_HOME",),
        "local": ("LOCALAPPDATA",),
        "roaming": ("APPDATA",),
        "home": ("HOME", "USERPROFILE"),
        "fastmcp": ("FASTMCP_HOME",),
        "matplotlib": ("MPLCONFIGDIR",),
        "legacy-cache": ("CLIO_KIT_CACHE_DIR",),
    }
    for directory, keys in groups.items():
        path = root / directory
        path.mkdir()
        environment.update({key: str(path) for key in keys})
    return root, environment


def install_candidate(package: str, root: Path, *, uv: str = "uv") -> tuple[Path, dict[str, str]]:
    """Install one candidate in a fresh, explicitly owned tool directory."""
    root, environment = prepare_runtime_root(root)
    subprocess.run(
        [uv, "tool", "install", "--force", science_package_spec(package)],
        check=True,
        env=environment,
    )
    suffix = ".exe" if os.name == "nt" else ""
    return root / "bin" / f"clio-kit{suffix}", environment


def read_runtime_info(launcher: str, environment: dict[str, str]) -> dict[str, Any]:
    """Read CLIO Kit's bounded installed-runtime identity report."""
    completed = subprocess.run(
        [launcher, "runtime-info", *SERVERS],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )
    data = json.loads(completed.stdout)
    if not isinstance(data, dict):
        raise RuntimeError("clio-kit runtime-info did not return a JSON object")
    return data


async def discover_tools(launcher: str, environment: dict[str, str]) -> list[dict[str, Any]]:
    """Start four separate processes and return their MCP discovery evidence."""
    from fastmcp.client.transports import StdioTransport  # noqa: PLC0415

    from fastmcp import Client  # noqa: PLC0415

    results: list[dict[str, Any]] = []
    for server in SERVERS:
        started = time.perf_counter()
        transport = StdioTransport(command=launcher, args=["mcp-server", server], env=environment)
        async with asyncio.timeout(60), Client(transport) as client:
            tools = await client.list_tools()
        names = sorted(tool.name for tool in tools)
        results.append(
            {
                "server": server,
                "tool_count": len(tools),
                "tools": names,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }
        )
    return results


async def exercise_csv_tools(
    launcher: str, work_dir: Path, environment: dict[str, str]
) -> list[dict[str, Any]]:
    """Profile and plot real CSV data through separate installed-server processes."""
    from fastmcp.client.transports import StdioTransport  # noqa: PLC0415

    from fastmcp import Client  # noqa: PLC0415

    csv = work_dir / "data.csv"
    output = work_dir / "plot.png"
    csv.write_text("x,y\n1,2\n2,4\n3,6\n", encoding="utf-8")
    calls = (
        ("pandas", "profile_csv", {"data_path": str(csv)}),
        (
            "plot",
            "line_plot",
            {
                "file_path": str(csv),
                "x_column": "x",
                "y_column": "y",
                "output_path": str(output),
            },
        ),
    )
    evidence: list[dict[str, Any]] = []
    for server, tool, arguments in calls:
        transport = StdioTransport(command=launcher, args=["mcp-server", server], env=environment)
        async with asyncio.timeout(60), Client(transport) as client:
            result = await client.call_tool(tool, arguments)
        if result.is_error:
            raise RuntimeError(f"{server}.{tool} returned an MCP error: {result.content}")
        evidence.append({"server": server, "tool": tool, "is_error": False})
    if not output.is_file() or not output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("plot.line_plot did not create a valid PNG")
    return evidence


def cache_snapshot(path: Path) -> tuple[int, int]:
    """Return file count and logical bytes below a legacy cache path."""
    if not path.exists():
        return (0, 0)
    files = [item for item in path.rglob("*") if item.is_file()]
    return (len(files), sum(item.stat().st_size for item in files))


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--package",
        help="base package or local candidate .whl to install with the science extra",
    )
    parser.add_argument("--launcher", help="installed clio-kit executable (defaults to PATH)")
    parser.add_argument(
        "--runtime-root",
        type=Path,
        required=True,
        help="fresh owned tool/cache/work root (must not already exist)",
    )
    parser.add_argument(
        "--legacy-cache",
        type=Path,
        default=None,
        help="legacy isolated-cache path checked for warm-launch growth",
    )
    return parser.parse_args()


def main() -> int:
    """Install an optional candidate, then qualify identity and MCP discovery."""
    args = parse_args()
    launcher: str | None
    if args.package:
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("uv is required to install a candidate package")
        installed, environment = install_candidate(args.package, args.runtime_root, uv=uv)
        launcher = str(installed)
    else:
        launcher = args.launcher or shutil.which("clio-kit")
        _, environment = prepare_runtime_root(args.runtime_root)
    if not launcher:
        raise RuntimeError(
            "clio-kit is not on PATH; install one persistent science runtime with "
            "the released launcher or an owned candidate science runtime"
        )
    # FastMCP is imported lazily below. Apply the owned environment first so its
    # parent-process version cache follows the same qualification root as children.
    os.environ.update(environment)

    legacy_cache = args.legacy_cache or Path(environment["CLIO_KIT_CACHE_DIR"])
    before = cache_snapshot(legacy_cache)
    runtime = read_runtime_info(launcher, environment)
    if runtime.get("schema_version") != RUNTIME_SCHEMA:
        raise RuntimeError(
            f"candidate does not expose {RUNTIME_SCHEMA}; got {runtime.get('schema_version')!r}"
        )
    rows = runtime.get("servers", {})
    missing_rows = sorted(set(SERVERS) - set(rows))
    if missing_rows:
        raise RuntimeError(f"runtime-info omitted requested servers: {missing_rows}")
    problems = {
        name: row.get("problems", [])
        for name, row in runtime.get("servers", {}).items()
        if row.get("problems")
    }
    if problems:
        raise RuntimeError(
            f"shared runtime has missing or incompatible dependencies: {problems}; "
            f"reinstall {science_package_spec(args.package or 'the candidate wheel')}"
        )
    work_parent = args.runtime_root / "runs"
    work_parent.mkdir()
    with tempfile.TemporaryDirectory(prefix="shared-mcp-", dir=work_parent) as work:
        discoveries = asyncio.run(discover_tools(launcher, environment))
        tool_calls = asyncio.run(exercise_csv_tools(launcher, Path(work), environment))
    after = cache_snapshot(legacy_cache)
    report = {
        "runtime": runtime,
        "discoveries": discoveries,
        "tool_calls": tool_calls,
        "legacy_cache_before": before,
        "legacy_cache_after": after,
        "legacy_cache_growth": after != before,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    missing_tools = {
        row["server"]: EXPECTED_TOOLS[row["server"]]
        for row in discoveries
        if EXPECTED_TOOLS[row["server"]] not in row["tools"]
    }
    if missing_tools:
        raise RuntimeError(f"MCP namespaces are missing expected tools: {missing_tools}")
    if after != before:
        raise RuntimeError("normal discovery changed the legacy isolated-runtime cache")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
