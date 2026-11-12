"""Probe the prepared interpreter rather than infer installed capabilities."""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import json
import platform
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from node_paths import node_path
from process import DocumentError, find_native, run

PACKAGES = {
    "filelock": "filelock",
    "truststore": "truststore",
    "psutil": "psutil",
    "pymupdf": "pymupdf",
    "pypdf": "pypdf",
    "pdfplumber": "pdfplumber",
    "reportlab": "reportlab",
    "python-docx": "docx",
    "python-pptx": "pptx",
    "openpyxl": "openpyxl",
    "odfpy": "odf",
    "pillow": "PIL",
    "numpy": "numpy",
    "pandas": "pandas",
    "matplotlib": "matplotlib",
    "lxml": "lxml.etree",
    "defusedxml": "defusedxml",
    "nodejs-wheel-binaries": "nodejs_wheel",
}

NATIVE_TOOLS = ("soffice", "pandoc", "pdftoppm", "tesseract")

#: Seconds a version probe may take. LibreOffice's cold start loads thousands of
#: freshly installed files: 15.8 s was measured on Windows with Defender on, after
#: which ``--version`` answers in 0.4 s. A shorter limit reports a working Office as
#: failed, and the agent then gives up on conversion.
VERSION_PROBE_TIMEOUT = {"soffice": 60}


def execution_host_inventory() -> dict[str, Any]:
    """Inspect existing packages and tools without preparing a document stack."""
    packages: dict[str, Any] = {}
    for distribution, module in PACKAGES.items():
        try:
            version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            packages[distribution] = {"status": "missing", "import": module}
        else:
            packages[distribution] = {
                "status": "available",
                "version": version,
                "import": module,
                "verification": "installed_metadata",
            }
    node = shutil.which("node")
    return {
        "python": sys.executable,
        "python_version": platform.python_version(),
        "inventory_scope": "execution_host",
        "os": platform.system(),
        "architecture": platform.machine(),
        "packages": packages,
        "node": node,
        "javascript": {
            "status": "available" if node else "missing",
            "node_argv": [node] if node else [],
        },
    }


def _native_tool(name: str, cwd: Path) -> dict[str, Any]:
    """Discover one optional converter and retain its bounded execution failure."""
    try:
        executable = find_native(name)
        result: dict[str, Any] = {
            "status": "available" if executable else "missing",
            "path": executable,
        }
        if executable:
            version_flag = "-v" if name == "pdftoppm" else "--version"
            result["version"] = run(
                [executable, version_flag], cwd=cwd, timeout=VERSION_PROBE_TIMEOUT.get(name, 10)
            ).strip()[:300]
        return result
    except DocumentError as exc:
        return {"status": "failed", "error": str(exc)}


def native_inventory(cwd: Path) -> dict[str, dict[str, Any]]:
    """Probe the independent converters concurrently."""
    with ThreadPoolExecutor(
        max_workers=len(NATIVE_TOOLS), thread_name_prefix="document-native-probe"
    ) as pool:
        futures = {name: pool.submit(_native_tool, name, cwd) for name in NATIVE_TOOLS}
        return {name: future.result() for name, future in futures.items()}


def _import_packages() -> None:
    """Import every managed package, several at once.

    The first load of a newly installed native library waits on the OS's file check,
    so the packages load in parallel threads. A failure there (including a concurrent
    circular import) is retried alone, which gives the authoritative error.
    """
    modules = list(PACKAGES.values())
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="document-import-probe") as pool:
        failed = [
            module
            for module, future in zip(
                modules, [pool.submit(importlib.import_module, m) for m in modules], strict=True
            )
            if future.exception() is not None
        ]
    for module in failed:
        importlib.import_module(module)


def _font_inventory() -> tuple[list[str], set[str], bool]:
    fonts: list[str] = []
    roots = [Path("/usr/share/fonts"), Path("/Library/Fonts"), Path("C:/Windows/Fonts")]
    for root in roots:
        if root.is_dir():
            fonts.extend(
                str(path)
                for path in root.rglob("*")
                if path.suffix.lower() in {".ttf", ".otf", ".ttc"}
            )
    selected_fonts = sorted(fonts, key=str.casefold)[:150]
    families: set[str] = set()
    if selected_fonts:
        from PIL import ImageFont

        for font in selected_fonts:
            try:
                family = ImageFont.truetype(font).getname()[0]
                if family:
                    families.add(family)
            except OSError:
                continue
    return selected_fonts, families, len(fonts) > 150


def _node(node_module_file: str | None) -> tuple[Path, Path]:
    """The managed Node.js package's root and executable."""
    if not node_module_file:
        raise DocumentError("Managed Node.js package has no module file")
    node_root = Path(node_path(str(Path(node_module_file).resolve().parent)))
    node_candidates = [
        node_root / "bin" / "node",
        node_root / "node.exe",
        node_root / "bin" / "node.exe",
    ]
    node = next((path for path in node_candidates if path.is_file()), None)
    if node is None:
        raise DocumentError("Managed Node.js package has no supported executable layout")
    return node_root, node


def _node_version(node_root: Path, node: Path) -> str:
    return run([str(node), "--version"], cwd=node_root).strip()


def inventory(*, execution_only: bool = False) -> dict[str, Any]:
    """Inspect current packages and tools, with optional lightweight module discovery."""
    nodejs_spec = importlib.util.find_spec("nodejs_wheel")
    node_root, node = _node(nodejs_spec.origin if nodejs_spec else None)
    if execution_only:
        for module in PACKAGES.values():
            if importlib.util.find_spec(module) is None:
                raise ImportError(f"Managed package module is missing: {module}")
        node_version = _node_version(node_root, node)
        native = {name: {"status": "not_checked"} for name in NATIVE_TOOLS}
        selected_fonts: list[str] = []
        families: set[str] = set()
        truncated = False
    else:
        # The converters, Node.js and the font scan start at once, while the packages
        # import: each is mostly waiting on a process start or a first file check.
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="document-probe") as pool:
            version_future = pool.submit(_node_version, node_root, node)
            native_future = pool.submit(native_inventory, node_root)
            fonts_future = pool.submit(_font_inventory)
            _import_packages()
            node_version = version_future.result()
            native = native_future.result()
            selected_fonts, families, truncated = fonts_future.result()
    packages: dict[str, Any] = {
        distribution: {
            "version": importlib.metadata.version(distribution),
            "import": module,
            "verification": "module_discovered" if execution_only else "imported",
        }
        for distribution, module in PACKAGES.items()
    }
    return {
        "python": sys.executable,
        "inventory_scope": "execution" if execution_only else "documents",
        "python_version": platform.python_version(),
        "os": platform.system(),
        "architecture": platform.machine(),
        "packages": packages,
        "node": str(node),
        "node_version": node_version,
        "native_tools": native,
        "font_files": selected_fonts,
        "font_families": sorted(families),
        "fonts_truncated": truncated,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-only", action="store_true")
    parser.add_argument("--execution-host", action="store_true")
    arguments = parser.parse_args()
    result = (
        execution_host_inventory()
        if arguments.execution_host
        else inventory(execution_only=arguments.execution_only)
    )
    print(json.dumps(result, ensure_ascii=False))
