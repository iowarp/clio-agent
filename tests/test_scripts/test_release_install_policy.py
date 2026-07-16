"""Release-install policy tests for CLIO's intentional DSPy prerelease.

DSPy 3.3.0b1 is a transitive dependency of a published clio-agent wheel.
uv deliberately ignores transitive prereleases unless the consuming command
opts in, which made the v0.7.5 wheel unsatisfiable through the documented uv
installer. These tests keep every supported install path and the release smoke
on the same explicit policy.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_VERSION = "0.7.6"


def _text(relative_path: str) -> str:
    """Return one repository file as UTF-8 text."""

    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_release_installers_explicitly_allow_transitive_prereleases() -> None:
    """Every official uv path that installs CLIO opts into DSPy 3.3.0b1."""

    expected_commands = {
        "install/install.sh": (
            "uv sync --prerelease allow --extra argonne",
            'uv pip install --prerelease allow --quiet --python "$VENV/bin/python"',
        ),
        "install/install.ps1": (
            "RunNative uv @('sync', '--prerelease', 'allow')",
            "RunNative uv @('pip', 'install', '--prerelease', 'allow'",
        ),
        "install/clio": (
            'pip install --prerelease allow --python "$CLIO_VENV/bin/python" -U clio-agent',
        ),
        "install/build-gact-runtime.sh": (
            'uv pip install --prerelease allow --python "$OUT/$PYBIN_REL" "$SPEC"',
        ),
        "install/build-gact-runtime.ps1": (
            "@('pip', 'install', '--prerelease', 'allow', '--python', $pyBin, $spec)",
        ),
    }

    for relative_path, commands in expected_commands.items():
        contents = _text(relative_path)
        for command in commands:
            assert command in contents, f"{relative_path} lacks prerelease opt-in: {command}"


def test_release_workflow_smokes_the_built_wheel_before_publish() -> None:
    """The tag workflow installs the wheel with registry deps before publishing it."""

    workflow = _text(".github/workflows/release.yml")
    build = workflow.index("uv build")
    smoke = workflow.index('uv tool install --python 3.12 --no-cache --prerelease allow "$wheel"')
    import_check = workflow.index('"$UV_TOOL_BIN_DIR/clio-agent" --help')
    publish = workflow.index("run: uv publish", import_check)

    assert build < smoke < import_check < publish
    assert 'export UV_TOOL_DIR="$smoke_root/tools"' in workflow
    assert 'export UV_TOOL_BIN_DIR="$smoke_root/bin"' in workflow
    assert "-name 'clio_agent-*.whl'" in workflow


def test_documented_persistent_uv_tool_install_has_the_same_policy() -> None:
    """User-facing docs prefer persistent uv tools and include the beta opt-in."""

    command = f"uv tool install --prerelease allow clio-agent=={EXPECTED_VERSION}"
    for relative_path in ("README.md", "docs/INSTALL.md", "install/README.md"):
        contents = _text(relative_path)
        assert command in contents
        assert "uvx" in contents or "uv tool run" in contents


def test_package_init_and_lock_share_the_release_version() -> None:
    """The package metadata, import surface, and lock all identify v0.7.6."""

    pyproject = tomllib.loads(_text("pyproject.toml"))
    assert pyproject["project"]["version"] == EXPECTED_VERSION

    init_tree = ast.parse(_text("src/clio_agent/__init__.py"))
    init_versions = [
        node.value.value
        for node in init_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    assert init_versions == [EXPECTED_VERSION]

    lock = _text("uv.lock")
    package_record = f'[[package]]\nname = "clio-agent"\nversion = "{EXPECTED_VERSION}"'
    assert package_record in lock


def test_source_and_ci_sync_commands_are_explicit_about_prereleases() -> None:
    """Contributor and CI source installs preserve the same dependency policy."""

    for relative_path in (
        "AGENTS.md",
        "docs/CONTRIBUTOR_QUICKSTART.md",
        ".github/workflows/ci.yml",
        ".github/workflows/mutation.yml",
    ):
        contents = _text(relative_path)
        for line in contents.splitlines():
            if "uv sync" in line:
                assert "--prerelease allow" in line, relative_path
