"""Release-install policy tests for CLIO's pinned dependency boundary.

Locked source installs resolve CLIO's exact dependencies, while uv's registry-backed
resolver needs each intentional prerelease declared as an explicit root. The tested
LiteLLM wheel stays exact.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_VERSION = "0.9.2"
EXPECTED_DSPY = "dspy==3.3.0b1"
EXPECTED_FASTMCP = "fastmcp==4.0.0b5"
EXPECTED_FASTMCP_SLIM = "fastmcp-slim==4.0.0b5"
EXPECTED_FASTMCP_TASKS = "fastmcp-tasks==4.0.0b5"
EXPECTED_LITELLM = "litellm==1.91.3"


def _text(relative_path: str) -> str:
    """Return one repository file as UTF-8 text."""

    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_release_installers_explicitly_root_intentional_prereleases() -> None:
    """Every official uv path admits exact betas without a global prerelease policy."""

    expected_commands = {
        "install/install.sh": (
            "uv sync --extra argonne",
            '"dspy==3.3.0b1" "fastmcp==4.0.0b5" "fastmcp-slim==4.0.0b5"',
        ),
        "install/install.ps1": (
            "RunNative uv @('sync')",
            "'fastmcp-slim==4.0.0b5', 'fastmcp-tasks==4.0.0b5'",
        ),
        "install/clio": (
            '"dspy==3.3.0b1" "fastmcp==4.0.0b5" "fastmcp-slim==4.0.0b5"',
        ),
        "install/build-gact-runtime.sh": (
            '"dspy==3.3.0b1" "fastmcp==4.0.0b5" "fastmcp-slim==4.0.0b5"',
        ),
        "install/build-gact-runtime.ps1": (
            "'fastmcp-slim==4.0.0b5', 'fastmcp-tasks==4.0.0b5'",
        ),
    }

    for relative_path, commands in expected_commands.items():
        contents = _text(relative_path)
        assert "--prerelease" not in contents, relative_path
        for command in commands:
            assert command in contents, f"{relative_path} lacks narrow DSPy install: {command}"


def test_project_pins_intentional_prereleases_and_stable_litellm() -> None:
    """The package pins its intentional prereleases and tested provider wheel."""

    pyproject = tomllib.loads(_text("pyproject.toml"))
    dependencies = pyproject["project"]["dependencies"]
    assert EXPECTED_DSPY in dependencies
    assert EXPECTED_FASTMCP in dependencies
    assert EXPECTED_FASTMCP_SLIM not in dependencies
    assert EXPECTED_FASTMCP_TASKS in dependencies
    assert EXPECTED_LITELLM in dependencies


def test_release_workflow_smokes_the_built_wheel_before_publish() -> None:
    """The tag workflow installs the wheel with registry deps before publishing it."""

    workflow = _text(".github/workflows/release.yml")
    build = workflow.index("uv build")
    smoke = workflow.index('uv tool install --python 3.12 --no-cache "$wheel"')
    version_check = workflow.index('"$UV_TOOL_BIN_DIR/clio-agent" --version')
    publish = workflow.index("run: uv publish", version_check)

    assert build < smoke < version_check < publish
    assert 'export UV_TOOL_DIR="$smoke_root/tools"' in workflow
    assert 'export UV_TOOL_BIN_DIR="$smoke_root/bin"' in workflow
    assert "-name 'clio_agent-*.whl'" in workflow
    assert "PyPI already has the identical" in workflow
    assert "local_members != remote_members" in workflow
    assert "zipfile.ZipFile" in workflow
    assert "if: steps.pypi-artifact.outputs.exists != 'true'" in workflow


def test_release_builds_follow_the_current_gact_workspace_layout() -> None:
    """Bundle and container builders consume the released root pnpm workspace."""

    bundles = _text(".github/workflows/clio-bundles.yml")
    web_image = _text("docker/Dockerfile.clio-web")
    tui_builder = _text("scripts/build_clio_tui.sh")
    launcher = _text("install/clio")

    for contents in (bundles, web_image, launcher):
        assert "external/gact-tui/apps" not in contents
        assert "@clio/web" not in contents
        assert "@clio/workspace" in contents
    assert 'for cand in "$HOME/gact-tui"' in launcher
    assert 'GOWORK=off go build' in tui_builder


def test_release_workflow_smokes_the_published_registry_tool() -> None:
    """After publish, CI enables the pinned betas for the registry install."""

    workflow = _text(".github/workflows/release.yml")
    publish = workflow.index("run: uv publish")
    registry_job = workflow.index("registry-smoke:")
    registry_install = workflow.index(
        "uv tool install --python 3.12 --no-cache", registry_job
    )

    assert publish < registry_job < registry_install
    assert "needs: pypi" in workflow[registry_job:registry_install]
    assert "--with fastmcp==4.0.0b5" in workflow
    assert "--with fastmcp-slim==4.0.0b5" in workflow
    assert "--with fastmcp-tasks==4.0.0b5" in workflow
    assert "assert dspy.__version__ == '3.3.0b1'" in workflow
    assert "assert hasattr(dspy, 'ReActV2')" in workflow


def test_documented_persistent_uv_tool_install_has_the_same_policy() -> None:
    """User-facing registry installs enable the package's pinned prereleases."""

    command = (
        f"uv tool install --with {EXPECTED_DSPY} --with {EXPECTED_FASTMCP} "
        f"--with {EXPECTED_FASTMCP_SLIM} "
        f"--with {EXPECTED_FASTMCP_TASKS} clio-agent=={EXPECTED_VERSION}"
    )
    for relative_path in ("README.md", "docs/INSTALL.md", "install/README.md"):
        contents = _text(relative_path)
        assert command in contents
        assert "uvx" in contents or "uv tool run" in contents


def test_package_init_and_lock_share_the_release_version() -> None:
    """The package metadata, import surface, and lock all identify v0.9.1."""

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


def test_source_and_ci_sync_commands_keep_uv_stable_only() -> None:
    """Contributor and CI source installs preserve the same dependency policy."""

    for relative_path in (
        "AGENTS.md",
        "docs/CONTRIBUTOR_QUICKSTART.md",
        ".github/workflows/ci.yml",
        ".github/workflows/mutation.yml",
    ):
        contents = _text(relative_path)
        assert "--prerelease" not in contents, relative_path
        for line in contents.splitlines():
            if "uv sync" in line:
                assert "--prerelease" not in line, relative_path
