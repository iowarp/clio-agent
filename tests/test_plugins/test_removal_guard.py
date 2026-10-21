"""The blueprint system and the declared-workflow runner stay removed.

The removal manifest (``docs/design/sdk/removal-manifest.md``) is the single
source: every module in its **Removed** tables must be gone from disk, nothing in
``src/``, ``tests/`` or ``scripts/`` may import it, and the symbols it lists as
removed from retained modules must not be defined there again.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "design" / "sdk" / "removal-manifest.md"
_ROW_PATH = re.compile(r"^\| `(src/clio_agent/[^`]+\.py)`")
_SYMBOL_ROW = re.compile(r"^\| `(gact/[^`]+\.py|gact/[^`]+/)` \| (.+?) \|")


def _manifest() -> str:
    return MANIFEST.read_text(encoding="utf-8")


def _removed_modules() -> list[str]:
    text = _manifest()
    removed = text.split("## Rewritten", 1)[0]
    return sorted({m.group(1) for line in removed.splitlines() if (m := _ROW_PATH.match(line))})


def _dotted(path: str) -> str:
    return path.removeprefix("src/").removesuffix(".py").replace("/", ".")


def _removed_symbols() -> list[tuple[str, str]]:
    text = _manifest()
    table = text.split("### Removed symbols in retained modules", 1)[1].split("## ", 1)[0]
    rows: list[tuple[str, str]] = []
    for line in table.splitlines():
        match = _SYMBOL_ROW.match(line)
        if not match or match.group(1).endswith("/"):
            continue
        for symbol in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", match.group(2)):
            rows.append((match.group(1), symbol))
    return rows


def _python_files() -> list[Path]:
    files: list[Path] = []
    for folder in ("src", "tests", "scripts"):
        files.extend(p for p in (ROOT / folder).rglob("*.py") if "__pycache__" not in p.parts)
    return files


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = ".".join(path.relative_to(ROOT / "src").with_suffix("").parts[:-1]) if (
        ROOT / "src"
    ) in path.parents else ""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level and package:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1] + ([base] if base else []))
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def test_manifest_lists_the_removed_modules() -> None:
    modules = _removed_modules()
    assert len(modules) >= 50, modules
    assert "src/clio_agent/gact/agent_blueprints.py" in modules
    assert "src/clio_agent/gact/workflows.py" in modules


@pytest.mark.parametrize("module", _removed_modules())
def test_removed_module_is_gone(module: str) -> None:
    assert not (ROOT / module).exists(), f"{module} is listed as removed but still exists"


def test_nothing_imports_a_removed_module() -> None:
    removed = {_dotted(m) for m in _removed_modules()}
    offenders = []
    for path in _python_files():
        for name in _imports(path):
            if name in removed or any(name.startswith(f"{m}.") for m in removed):
                offenders.append(f"{path.relative_to(ROOT)} imports {name}")
    assert not offenders, "\n".join(offenders)


def test_removed_symbols_stay_removed() -> None:
    offenders = []
    for relative, symbol in _removed_symbols():
        path = ROOT / "src" / "clio_agent" / relative
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        } | {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        if symbol in defined:
            offenders.append(f"{relative} still defines {symbol}")
    assert not offenders, "\n".join(offenders)


def test_builtin_skills_folder_is_gone() -> None:
    assert not (ROOT / "src" / "clio_agent" / "gact" / "builtin_skills").exists()


def test_no_agent_md_blueprint_fixtures_remain() -> None:
    leftovers = [
        str(p.relative_to(ROOT))
        for folder in ("src", "tests")
        for p in (ROOT / folder).rglob("AGENT.md")
    ]
    assert not leftovers, leftovers
