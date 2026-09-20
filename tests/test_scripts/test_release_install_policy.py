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
EXPECTED_VERSION = "0.9.4.6"
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
        "install/clio": ('"dspy==3.3.0b1" "fastmcp==4.0.0b5" "fastmcp-slim==4.0.0b5"',),
        "install/build-gact-runtime.sh": (
            '"dspy==3.3.0b1" "fastmcp==4.0.0b5" "fastmcp-slim==4.0.0b5"',
        ),
        "install/build-gact-runtime.ps1": ("'fastmcp-slim==4.0.0b5', 'fastmcp-tasks==4.0.0b5'",),
    }

    for relative_path, commands in expected_commands.items():
        contents = _text(relative_path)
        assert "--prerelease" not in contents, relative_path
        for command in commands:
            assert command in contents, f"{relative_path} lacks narrow DSPy install: {command}"


def test_launchers_root_runtime_and_cte_state_under_the_selected_install() -> None:
    """Detached agents must not share stale host-global coordination paths."""

    shell = _text("install/clio")
    assert 'CLIO_DATA_DIR="${CLIO_DATA_DIR:-$CLIO_PREFIX/data}"' in shell
    assert 'CLIO_ARC_CTE_DIR="${CLIO_ARC_CTE_DIR:-$CLIO_PREFIX/cte}"' in shell
    assert 'CLIO_RUNTIME_STATE_DIR="${CLIO_RUNTIME_STATE_DIR:-$CLIO_PREFIX/runtime-state}"' in shell
    assert 'port_file="$CLIO_PREFIX/clio-core.port"' in shell
    assert 'export CLIO_CORE_PORT="$chosen"' in shell
    assert "unset CLIO_PORT" in shell

    powershell = _text("install/clio.ps1")
    assert "$env:CLIO_DATA_DIR = Join-Path $Prefix 'data'" in powershell
    assert "$env:CLIO_ARC_CTE_DIR = Join-Path $Prefix 'cte'" in powershell
    assert "$env:CLIO_RUNTIME_STATE_DIR = Join-Path $Prefix 'runtime-state'" in powershell
    assert "Remove-Item Env:CLIO_PORT" in powershell


def test_posix_launcher_allows_active_turns_to_drain_before_force_kill() -> None:
    """The fallback SIGKILL must not preempt runtime release during a busy stop."""

    shell = _text("install/clio")
    stop = shell[shell.index("stop_server() {") : shell.index("status_server() {")]
    assert "for i in $(seq 1 120)" in stop
    assert stop.index("kill -TERM") < stop.index("for i in $(seq 1 120)")
    assert stop.index("for i in $(seq 1 120)") < stop.index("kill -9")


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
    tui_image = _text("docker/Dockerfile.clio-tui")
    docker_workflow = _text(".github/workflows/docker.yml")
    tui_builder = _text("scripts/build_clio_tui.sh")
    launcher = _text("install/clio")

    for contents in (bundles, web_image, launcher):
        assert "external/gact-tui/apps" not in contents
        assert "@clio/web" not in contents
        assert "@clio/workspace" in contents
    assert 'for cand in "$HOME/gact-tui"' in launcher
    assert "GOWORK=off go build" in tui_builder
    assert '"$GACT_ROOT/package.json"' in tui_builder
    assert 'gact_release="v${gact_package_version}"' in tui_builder
    assert ".Release=${gact_release}" in tui_builder
    assert ".BuildRevision=${gact_revision}" in tui_builder
    assert 'gact_revision="${GACT_TUI_REVISION:-}"' in tui_builder
    assert 'gact_build_time="${GACT_TUI_BUILD_TIME:-}"' in tui_builder
    assert "ARG GACT_TUI_REVISION" in tui_image
    assert "ARG GACT_TUI_BUILD_TIME" in tui_image
    assert "Resolve GACT source identity" in docker_workflow
    assert "GACT_TUI_REVISION=${{ steps.gact_source.outputs.revision }}" in docker_workflow
    assert "GACT_TUI_BUILD_TIME=${{ steps.gact_source.outputs.build_time }}" in docker_workflow
    assert 'go version -m "$OUT"' in tui_builder
    assert '"$target_goos" == "$(go env GOHOSTOS)"' in tui_builder
    assert '"$target_goarch" == "$(go env GOHOSTARCH)"' in tui_builder
    assert '"$OUT" version' in tui_builder
    assert '"$OUT" version >/dev/null 2>&1 || true' not in tui_builder
    assert 'release_version="${GITHUB_REF_NAME#v}"' in bundles
    assert "config.version = tauriVersion" in bundles
    assert "`${maintenance[1]}+${maintenance[2]}`" in bundles
    assert "+patch.${maintenance[2]}" not in bundles
    assert "config.bundle.windows.wix.version = releaseVersion" in bundles
    assert 'base="${base//$tauri_version/$release_version}"' in bundles
    assert "invalid CLIO release version" in bundles


def test_bundled_runtime_is_precompiled_before_relocation_proof() -> None:
    """Official bundles pay Python compilation cost before installation."""

    precompiler = _text("install/precompile_runtime.py")
    assert "build_app()" in precompiler
    assert "PycInvalidationMode.UNCHECKED_HASH" in precompiler

    for relative_path in (
        "install/build-gact-runtime.sh",
        "install/build-gact-runtime.ps1",
    ):
        script = _text(relative_path)
        compile_step = script.index("compiling portable startup bytecode")
        relocation_proof = script.index("portability proof on the real object")
        assert compile_step < relocation_proof, relative_path
        assert "precompile_runtime.py" in script, relative_path
        assert "'--no-agent'" in script or '"--no-agent"' in script, relative_path
        assert "within 30 seconds" in script, relative_path
        assert "clio-kit==2.10.6" in script, relative_path
        assert "from clio_kit import cli; cli()" in script, relative_path
        assert "uvx" in script, relative_path

    windows_builder = _text("install/build-gact-runtime.ps1")
    assert "codex_cli_bin\\bin\\codex.exe" in windows_builder
    assert "packaged Codex provider executable is missing" in windows_builder
    assert "iowarp_core\\bin" in windows_builder
    assert "packaged clio-core launcher is missing" in windows_builder
    assert "initialize clio-core store" in windows_builder
    assert "install/arc_smoke.py" in windows_builder

    arc_smoke = _text("install/arc_smoke.py")
    assert "isinstance(store, ClioCoreStore)" in arc_smoke


def test_release_workflow_smokes_the_published_registry_tool() -> None:
    """After publish, CI enables the pinned betas for the registry install."""

    workflow = _text(".github/workflows/release.yml")
    publish = workflow.index("run: uv publish")
    registry_job = workflow.index("registry-smoke:")
    registry_install = workflow.index("uv tool install --python 3.12 --no-cache", registry_job)

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
    for relative_path in ("docs/INSTALL.md", "install/README.md"):
        contents = _text(relative_path)
        assert command in contents
        assert "uvx" in contents or "uv tool run" in contents

    # The release README retains the broader, still-valid command
    # published to PyPI. Official installers and current install docs use the narrower
    # exact-root policy above.
    assert (
        f"uv tool install --prerelease allow --with dspy==3.3.0b1 clio-agent=={EXPECTED_VERSION}"
    ) in _text("README.md")


def test_package_init_and_lock_share_the_release_version() -> None:
    """The package metadata, import surface, and lock share the release version."""

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


def test_release_workflow_signs_and_publishes_the_update_manifest() -> None:
    """Signed desktop auto-update plumbing (v0.9.4.1, #A7) is wired into clio-bundles.yml."""

    bundles = _text(".github/workflows/clio-bundles.yml")

    # (f) workflow_dispatch frozen-at-tag escape hatch, decoupled from the
    # heavy build jobs (which stay push-only).
    assert "workflow_dispatch:" in bundles
    assert "tag:" in bundles
    assert "if: github.event_name == 'push'" in bundles
    assert "TAG: ${{ inputs.tag || github.ref_name }}" in bundles

    # (a) the merge script no longer forces createUpdaterArtifacts off or
    # strips the pubkey; every installed variant uses the lightweight feed.
    assert "config.bundle.createUpdaterArtifacts = false" not in bundles
    assert "delete config.plugins.updater.pubkey" not in bundles
    assert "latest-lite.json" in bundles
    assert (
        "https://github.com/iowarp/clio-agent/releases/latest/download/latest-lite.json" in bundles
    )

    # (b) the Tauri build step signs with the repo secrets.
    build_idx = bundles.index("name: Tauri release build")
    stage_idx = bundles.index("name: Stage artifacts")
    build_step = bundles[build_idx:stage_idx]
    assert "TAURI_SIGNING_PRIVATE_KEY: ${{ secrets.TAURI_SIGNING_PRIVATE_KEY }}" in build_step
    assert (
        "TAURI_SIGNING_PRIVATE_KEY_PASSWORD: ${{ secrets.TAURI_SIGNING_PRIVATE_KEY_PASSWORD }}"
        in build_step
    )

    # (d) the macOS decorations assert is guarded, never fatal when the
    # branded runner isn't pinned yet.
    assert "Assert macOS traffic lights survive the brand overlay" in bundles
    assert "run-tauri-branded.mjs not present in this gact-tui pin yet" in bundles

    # (c) staging also produces + renames .sig / .app.tar.gz, and excludes
    # .sig from the bundled payload floor.
    stage_end_idx = bundles.index("uses: softprops/action-gh-release@v2", stage_idx)
    stage_step = bundles[stage_idx:stage_end_idx]
    assert "-iname '*.sig'" in stage_step
    assert "-name '*.app.tar.gz'" in stage_step
    assert "-maxdepth 2 -type f" in stage_step  # never sweep deb work files or runtime internals
    assert "find \"$stage\" -type f -name '*-bundled.*' ! -name '*.sig' -print0" in stage_step
    assert 'base="${base//$tauri_version/$release_version}"' in stage_step

    # (e) release-check generates + uploads the manifest before the
    # completeness check, which now reads $TAG (not $GITHUB_REF_NAME).
    check_idx = bundles.index("name: release completeness")
    manifest_idx = bundles.index("name: Generate signed Tauri update manifest", check_idx)
    completeness_idx = bundles.index("name: Assert release asset completeness", manifest_idx)
    assert check_idx < manifest_idx < completeness_idx
    manifest_step = bundles[manifest_idx:completeness_idx]
    assert 'gen_tauri_update_manifest.py --tag "$TAG" --variant lite --out latest.json' in (
        manifest_step
    )
    assert 'gen_tauri_update_manifest.py --tag "$TAG" --variant lite --out latest-lite.json' in (
        manifest_step
    )
    assert 'gh release upload "$TAG" latest.json latest-lite.json --clobber' in manifest_step
    assert 'gh release view "$TAG" --json assets' in bundles
    assert 'gh release view "$GITHUB_REF_NAME"' not in bundles


def test_release_completeness_expects_signed_updater_assets() -> None:
    """check_release_completeness.py's EXPECTED_ASSETS covers the new signed-update assets."""

    from scripts.check_release_completeness import EXPECTED_ASSETS

    labels = {label for label, _ in EXPECTED_ASSETS}
    for expected_label in (
        "bundled nsis sig (x86_64 Windows)",
        "bundled macOS updater bundle (aarch64)",
        "bundled macOS updater sig (aarch64)",
        "lite nsis sig (x86_64 Windows)",
        "lite macOS updater bundle (aarch64)",
        "lite macOS updater sig (aarch64)",
        "lite macOS updater bundle (x86_64)",
        "lite macOS updater sig (x86_64)",
        "lite AppImage sig (x86_64 Linux)",
        "lite AppImage sig (aarch64 Linux)",
        "Tauri update manifest (bundled)",
        "Tauri update manifest (lite)",
    ):
        assert expected_label in labels


def test_clio_brand_overlay_declares_the_updater() -> None:
    """The CLIO Tauri brand overlay ships its own updater endpoint + pubkey."""

    import json

    overlay = json.loads(_text("branding/clio/tauri.clio.conf.json"))
    assert overlay["bundle"]["createUpdaterArtifacts"] is True
    updater = overlay["plugins"]["updater"]
    assert updater["endpoints"] == [
        "https://github.com/iowarp/clio-agent/releases/latest/download/latest-lite.json"
    ]
    assert updater["pubkey"]
    assert updater["windows"]["installMode"] == "passive"
