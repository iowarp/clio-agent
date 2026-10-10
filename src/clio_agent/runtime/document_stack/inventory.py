"""Probe the prepared interpreter rather than infer installed capabilities."""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import json
import platform
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
    "lxml": "lxml.etree",
    "defusedxml": "defusedxml",
    "nodejs-wheel-binaries": "nodejs_wheel",
}

NATIVE_TOOLS = ("soffice", "pandoc", "pdftoppm", "tesseract")


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
            result["version"] = run([executable, version_flag], cwd=cwd, timeout=10).strip()[:300]
        return result
    except DocumentError as exc:
        return {"status": "failed", "error": str(exc)}


def native_inventory(cwd: Path) -> dict[str, dict[str, Any]]:
    """Probe independent converters with at most two concurrent child processes."""
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="document-native-probe") as pool:
        futures = {name: pool.submit(_native_tool, name, cwd) for name in NATIVE_TOOLS}
        return {name: future.result() for name, future in futures.items()}


def inventory(*, execution_only: bool = False) -> dict[str, Any]:
    """Inspect current packages and tools, with optional lightweight module discovery."""
    packages: dict[str, Any] = {}
    for distribution, module in PACKAGES.items():
        if execution_only:
            if importlib.util.find_spec(module) is None:
                raise ImportError(f"Managed package module is missing: {module}")
        else:
            importlib.import_module(module)
        packages[distribution] = {
            "version": importlib.metadata.version(distribution),
            "import": module,
            "verification": "module_discovered" if execution_only else "imported",
        }
    import nodejs_wheel

    node_module_file = nodejs_wheel.__file__
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
    node_version = run([str(node), "--version"], cwd=node_root).strip()
    native = (
        {name: {"status": "not_checked"} for name in NATIVE_TOOLS}
        if execution_only
        else native_inventory(node_root)
    )
    fonts: list[str] = []
    roots = [Path("/usr/share/fonts"), Path("/Library/Fonts"), Path("C:/Windows/Fonts")]
    for root in [] if execution_only else roots:
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
        "fonts_truncated": len(fonts) > 150,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-only", action="store_true")
    arguments = parser.parse_args()
    print(json.dumps(inventory(execution_only=arguments.execution_only), ensure_ascii=False))
