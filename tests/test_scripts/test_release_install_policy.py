"""Release-install policy tests for CLIO's pinned dependency boundary.

Locked source installs resolve CLIO's exact dependencies, while uv's registry-backed
resolver needs each intentional prerelease declared as an explicit root. The tested
LiteLLM wheel stays exact.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_VERSION = "0.9.5b5.post2"
#: The release the install docs name: the latest stable one. A beta changes the
#: package version only; users opt into it explicitly.
DOCUMENTED_VERSION = "0.9.4.24"
EXPECTED_DSPY = "dspy==3.4.0"
EXPECTED_FASTMCP = "fastmcp==4.0.0b5"
EXPECTED_FASTMCP_SLIM = "fastmcp-slim==4.0.0b5"
EXPECTED_FASTMCP_TASKS = "fastmcp-tasks==4.0.0b5"
EXPECTED_LITELLM = "litellm==1.102.1"


def _text(relative_path: str) -> str:
    """Return one repository file as UTF-8 text."""

    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_release_installers_explicitly_root_intentional_prereleases() -> None:
    """Every official uv path admits exact betas without a global prerelease policy."""

    expected_commands = {
        "install/install.sh": (
            "uv sync --python 3.13 --extra argonne",
            '"dspy==3.4.0" "fastmcp==4.0.0b5" "fastmcp-slim==4.0.0b5"',
        ),
        "install/install.ps1": (
            "RunNative uv @('sync', '--python', '3.13')",
            "'fastmcp-slim==4.0.0b5', 'fastmcp-tasks==4.0.0b5'",
        ),
        "install/clio": ('"dspy==3.4.0" "fastmcp==4.0.0b5" "fastmcp-slim==4.0.0b5"',),
    }
    # The bundled-runtime builders root nothing themselves: they install
    # clio-agent[BUNDLE_EXTRAS] against the lock export, whose exact prerelease
    # pins root the betas (test_check_bundle_matches_lock.py covers them).

    for relative_path, commands in expected_commands.items():
        contents = _text(relative_path)
        assert "--prerelease" not in contents, relative_path
        for command in commands:
            assert command in contents, f"{relative_path} lacks narrow DSPy install: {command}"


def test_launchers_never_scope_the_host_global_clio_core_daemon() -> None:
    """One clio-core daemon per machine (owner ruling 2026-09-24): no launcher scopes it.

    The per-install state dir / CTE dir / port block made a spawned daemon compose a
    config the in-process client never read (ares, 2026-09-25: daemon on the install
    port, client waiting on 9413). Agent data still follows the install.
    """

    remote_driver = _text("src/clio_agent/gact/infrastructure/drivers.py") + _text(
        "src/clio_agent/gact/infrastructure/clio_agent_deploy.py"
    )
    for relative_path in ("install/clio", "install/clio.ps1"):
        contents = _text(relative_path)
        for scoped in ("CLIO_RUNTIME_STATE_DIR", "CLIO_ARC_CTE_DIR", "CLIO_CORE_PORT"):
            assert scoped not in contents, f"{relative_path} scopes the daemon via {scoped}"
            assert scoped not in remote_driver, f"remote driver scopes the daemon via {scoped}"
    assert "clio-core.port" not in _text("install/clio")

    shell = _text("install/clio")
    assert "export CLIO_DATA_DIR=" not in shell
    assert "user_state_dir" in shell
    assert "unset CLIO_PORT" in shell
    powershell = _text("install/clio.ps1")
    assert "$env:CLIO_DATA_DIR =" not in powershell
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
    smoke_step = workflow.index("- name: Smoke built wheel with registry-resolved dependencies")
    smoke = workflow.index("uv tool install --python 3.13 --no-cache", smoke_step)
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
    assert "`${beta[1]}-${beta[2]}`" in bundles
    assert 'tauri_version="${BASH_REMATCH[1]}-${BASH_REMATCH[2]}"' in bundles
    assert "+patch.${maintenance[2]}" not in bundles
    assert "config.bundle.windows.wix.version = releaseVersion" in bundles
    assert 'base="${base//$tauri_version/$release_version}"' in bundles
    assert "invalid CLIO release version" in bundles


def test_bundled_runtime_is_precompiled_before_relocation_proof() -> None:
    """Official bundles pay Python compilation cost before installation."""

    precompiler = _text("install/precompile_runtime.py")
    assert "build_app()" in precompiler
    # Checked, never unchecked: the desktop upgrades the runtime in place and
    # unchecked bytecode kept running the previous release's dependencies.
    assert "invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH" in precompiler
    assert "invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH" not in precompiler

    for relative_path in (
        "install/build-gact-runtime.sh",
        "install/build-gact-runtime.ps1",
    ):
        script = _text(relative_path)
        # RECORD lets `uv pip install` uninstall the previous version in place.
        assert "RECORD' -delete" not in script, relative_path
        assert "Join-Path $_.FullName 'RECORD'" not in script, relative_path
        compile_step = script.index("compiling portable startup bytecode")
        relocation_proof = script.index("portability proof on the real object")
        assert compile_step < relocation_proof, relative_path
        assert "precompile_runtime.py" in script, relative_path
        assert "'--no-agent'" in script or '"--no-agent"' in script, relative_path
        assert "within 30 seconds" in script, relative_path
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
    assert "except ArcStoreUnavailableError" in arc_smoke  # typed: clio-core or exit 1


def test_release_workflow_smokes_the_built_wheel_with_pinned_prereleases() -> None:
    """The pre-publish wheel install uses the official installer's narrow beta roots."""
    workflow = _text(".github/workflows/release.yml")
    start = workflow.index("- name: Smoke built wheel with registry-resolved dependencies")
    end = workflow.index("- name: Check for an identical existing PyPI artifact", start)
    smoke = workflow[start:end]
    assert '"$wheel"' in smoke
    for requirement in (
        EXPECTED_DSPY,
        EXPECTED_FASTMCP,
        EXPECTED_FASTMCP_SLIM,
        EXPECTED_FASTMCP_TASKS,
    ):
        assert f"--with {requirement}" in smoke
    assert "--prerelease" not in smoke


def test_release_workflow_smokes_the_published_registry_tool() -> None:
    """After publish, CI enables the pinned betas for the registry install."""

    workflow = _text(".github/workflows/release.yml")
    publish = workflow.index("run: uv publish")
    registry_job = workflow.index("registry-smoke:")
    registry_install = workflow.index("uv tool install --python 3.13 --no-cache", registry_job)

    assert publish < registry_job < registry_install
    assert "needs: pypi" in workflow[registry_job:registry_install]
    assert "--with fastmcp==4.0.0b5" in workflow[registry_install:]
    assert "--with fastmcp-slim==4.0.0b5" in workflow[registry_install:]
    assert "--with fastmcp-tasks==4.0.0b5" in workflow[registry_install:]
    assert "assert dspy.__version__ == '3.4.0'" in workflow
    assert "assert hasattr(dspy, 'lm15')" in workflow


def test_documented_persistent_uv_tool_install_has_the_same_policy() -> None:
    """User-facing registry installs enable the package's pinned prereleases."""

    command = (
        f"uv tool install --python 3.13 --with {EXPECTED_DSPY} --with {EXPECTED_FASTMCP} "
        f"--with {EXPECTED_FASTMCP_SLIM} "
        f"--with {EXPECTED_FASTMCP_TASKS} clio-agent=={DOCUMENTED_VERSION}"
    )
    for relative_path in ("docs/INSTALL.md", "install/README.md"):
        contents = _text(relative_path)
        assert command in contents
        assert "uvx" in contents or "uv tool run" in contents

    # The release README retains the broader, still-valid command
    # published to PyPI. Official installers and current install docs use the narrower
    # exact-root policy above.
    assert (
        f"uv tool install --python 3.13 --prerelease allow --with dspy==3.4.0 clio-agent=={DOCUMENTED_VERSION}"
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
    stage_end_idx = bundles.index("name: Upload to draft release", stage_idx)
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


def test_release_is_a_draft_until_release_check_publishes_it() -> None:
    """Draft-first lifecycle: a release is never "latest" before it is complete.

    v0.9.4.19's release was created by the first asset upload, became latest
    at once, and ``releases/latest/download/latest-lite.json`` 404'd for over
    an hour while the desktop legs built. Now ONE job creates the draft, every
    uploader waits for it, and only release-check publishes -- after the
    manifest + completeness steps.
    """

    import yaml

    bundles = _text(".github/workflows/clio-bundles.yml")
    workflow = yaml.safe_load(bundles)
    jobs = workflow["jobs"]

    # No uploader may create the release as a side effect any more.
    assert "softprops/action-gh-release" not in bundles
    assert "gh release create" not in bundles

    # One creator, and it creates a draft via github_release.py ensure.
    creators = [
        name
        for name, job in jobs.items()
        if any("github_release.py ensure" in str(step.get("run", "")) for step in job["steps"])
    ]
    assert creators == ["release"]
    assert jobs["release"]["if"] == "github.event_name == 'push'"

    # Every job that uploads waits for the creator (no concurrent create race).
    uploaders = [
        name
        for name, job in jobs.items()
        if any("upload_release_assets.sh" in str(step.get("run", "")) for step in job["steps"])
    ]
    assert sorted(uploaders) == ["desktop", "installers", "tui", "web"]
    for name in uploaders:
        assert jobs[name]["needs"] == "release", name

    # Runs for one tag are serialized, never cancelled mid-upload.
    assert workflow["concurrency"] == {
        "group": (
            "clio-bundles-${{ inputs.tag || github.ref_name }}"
            "${{ inputs.allow_pending_macos && '-beta-macos-exception' || '' }}"
        ),
        "cancel-in-progress": False,
    }

    # release-check: manifest -> completeness -> notes backstop -> publish -> verify feed.
    check = jobs["release-check"]
    assert "release" in check["needs"]
    names = [step.get("name", "") for step in check["steps"]]
    order = [
        "Generate signed Tauri update manifest",
        "Assert release asset completeness",
        "Backstop release notes from CHANGELOG",
        "Publish release (draft -> public, latest)",
        "Verify the public updater feed resolves to this release",
    ]
    assert [names.index(n) for n in order] == sorted(names.index(n) for n in order)
    publish_step = check["steps"][names.index(order[3])]
    assert publish_step["run"].strip() == 'python3 scripts/github_release.py publish --tag "$TAG"'
    assert "failure" in publish_step["if"] and "cancelled" in publish_step["if"]
    verify_step = check["steps"][names.index(order[4])]
    assert verify_step["if"] == "steps.publish.outputs.made_latest == 'true'"
    assert "releases/latest/download/latest-lite.json" in verify_step["run"]
    # Publishing happens nowhere else.
    publishers = [
        (job_name, step.get("name"))
        for job_name, job in jobs.items()
        for step in job["steps"]
        if "github_release.py publish" in str(step.get("run", ""))
        or "--draft=false" in str(step.get("run", ""))
    ]
    assert publishers == [("release-check", order[3])]


def test_release_macos_gate_can_read_and_recheck_draft_assets() -> None:
    """Draft verification has visibility and can reuse the exact built release assets."""
    workflow = yaml.safe_load(_text(".github/workflows/clio-bundles.yml"))
    jobs = workflow["jobs"]
    startup = jobs["macos-startup"]
    assert startup.get("permissions", {}).get("contents") == "write"
    assert "!cancelled()" in startup["if"]
    assert "inputs.verify_macos" in startup["if"]
    assert "failure" in startup["if"]
    assert startup["env"]["RELEASE_TAG"] == "${{ inputs.tag || github.ref_name }}"
    checkout = startup["steps"][0]
    assert checkout["with"]["ref"] == "${{ env.RELEASE_TAG }}"
    boot = next(step for step in startup["steps"] if "run" in step)
    assert boot["run"].count('gh release download "$RELEASE_TAG"') == 2
    assert "shasum -a 256 --check" in boot["run"]
    assert "smoke_macos_bundle.py" in boot["run"]

    # PyYAML's YAML 1.1 loader interprets the unquoted Actions key `on` as True.
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["workflow_dispatch"]["inputs"]["verify_macos"]["type"] == "boolean"
    publish_step = next(
        step for step in jobs["release-check"]["steps"] if step.get("id") == "publish"
    )
    # A failed requested startup check must block dispatch publication too.
    assert publish_step["if"].strip() == (
        "!contains(needs.*.result, 'failure') && !contains(needs.*.result, 'cancelled')"
    )


def test_beta_macos_exception_is_explicit_guarded_and_disclosed() -> None:
    """Only approved beta dispatch uses partial Mac manifests; other builds stay frozen."""
    workflow = yaml.safe_load(_text(".github/workflows/clio-bundles.yml"))
    triggers = workflow.get("on", workflow.get(True))
    flag = triggers["workflow_dispatch"]["inputs"]["allow_pending_macos"]
    assert flag["type"] == "boolean" and flag["default"] is False
    check = workflow["jobs"]["release-check"]
    assert "github.event_name == 'workflow_dispatch'" in check["env"]["ALLOW_PENDING_MACOS"]
    assert check["steps"][0]["with"]["ref"] == (
        "${{ inputs.allow_pending_macos && github.ref || env.TAG }}"
    )
    steps = {step.get("name"): step for step in check["steps"]}
    generate = steps["Generate signed Tauri update manifest"]["run"]
    assert generate.index("expected_assets(") < generate.index("gh release upload")
    assert "manifest_args=(--allow-missing darwin-aarch64,darwin-x86_64)" in generate
    completeness = steps["Assert release asset completeness"]["run"]
    assert 'completeness_args=(--tag "$TAG" --allow-missing-macos)' in completeness
    disclose = steps["Disclose deferred macOS Desktop qualification"]
    assert disclose["if"] == "env.ALLOW_PENDING_MACOS == 'true'"
    assert "macOS Desktop qualification is deferred for this beta" in disclose["run"]


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


def _verify_tag_step() -> str:
    """Return the release workflow's tag-vs-version check script."""

    workflow = yaml.safe_load(_text(".github/workflows/release.yml"))
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == "Verify tag matches package version":
                return str(step["run"])
    raise AssertionError("release.yml lost its tag-vs-version check")


def _run_tag_check(tmp_path: Path, tag: str, package_version: str) -> int:
    """Run the real check script with ``uv version --short`` answering ``package_version``."""

    bash = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
    assert bash is not None, "the release check is a bash script"

    def shell_path(path: Path) -> str:
        value = path.as_posix()
        return f"/{value[0].lower()}{value[2:]}" if os.name == "nt" else value

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uv = bin_dir / "uv"
    uv.write_text(f"#!/usr/bin/env bash\necho {package_version}\n", encoding="utf-8", newline="\n")
    uv.chmod(0o755)
    script = tmp_path / "check.sh"
    script.write_text(_verify_tag_step(), encoding="utf-8", newline="\n")
    for name in ("github_env", "github_output"):
        (tmp_path / name).write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "TEST_PATH": f"{shell_path(bin_dir)}:/usr/bin:/bin",
        "GITHUB_REF_NAME": tag,
        "GITHUB_ENV": shell_path(tmp_path / "github_env"),
        "GITHUB_OUTPUT": shell_path(tmp_path / "github_output"),
    }
    return subprocess.run(
        [
            bash,
            "-c",
            'export PATH="$TEST_PATH"; exec bash "$1"',
            "release-test",
            shell_path(script),
        ],
        env=env,
        check=False,
        timeout=10,
    ).returncode


def test_release_tag_check_accepts_a_beta_tag_for_its_pep440_version(tmp_path: Path) -> None:
    """vX.Y.Z-beta.N publishes the package version X.Y.ZbN; mismatches still fail."""

    assert _run_tag_check(tmp_path, "v0.9.5-beta.1", "0.9.5b1") == 0
    # Every later step (wheel smoke, PyPI lookup, registry smoke) names the
    # package by this exported version, never by the tag.
    assert (tmp_path / "github_env").read_text(encoding="utf-8") == "PKG_VERSION=0.9.5b1\n"
    assert (tmp_path / "github_output").read_text(encoding="utf-8") == "version=0.9.5b1\n"
    assert _run_tag_check(tmp_path, "v0.9.4.24", "0.9.4.24") == 0
    assert _run_tag_check(tmp_path, "v0.9.5-beta.2", "0.9.5b1") != 0
    assert _run_tag_check(tmp_path, "v0.9.5-beta.1", "0.9.5") != 0


@pytest.mark.parametrize(
    ("public", "package"),
    [("v0.9.5-beta.5.1", "0.9.5b5.post1"), ("v0.9.5-beta.5.2", "0.9.5b5.post2")],
)
def test_beta_hotfix_tag_matches_its_pep440_post_release(
    tmp_path: Path,
    public: str,
    package: str,
) -> None:
    """Each hotfix publishes its distinct, ordered Python version."""
    assert _run_tag_check(tmp_path, public, package) == 0
    assert (tmp_path / "github_output").read_text() == f"version={package}\n"
    assert _run_tag_check(tmp_path, public, "0.9.5b5") != 0


def test_actual_desktop_config_preserves_beta_hotfix_and_msi_order(tmp_path: Path) -> None:
    """Execute the real Node merge script for hotfix, next beta and stable tags."""
    import json
    import re

    workflow = yaml.safe_load(_text(".github/workflows/clio-bundles.yml"))
    steps = workflow["jobs"]["desktop"]["steps"]
    build = next(step["run"] for step in steps if step.get("name") == "Tauri release build")
    match = re.search(r"<<'NODE'\n(.*?)\nNODE", build, flags=re.DOTALL)
    assert match is not None
    for public, app, msi in (
        ("0.9.5-beta.5.1", "0.9.5-5+1", "0.9.5.5001"),
        ("0.9.5-beta.5.2", "0.9.5-5+2", "0.9.5.5002"),
        ("0.9.5-beta.6", "0.9.5-6", "0.9.5.6000"),
        ("0.9.4.24", "0.9.4+24", "0.9.4.24"),
    ):
        output = tmp_path / "config.json"
        result = subprocess.run(
            [
                "node",
                "-",
                "lite",
                str(ROOT / "branding/clio/tauri.clio.conf.json"),
                str(output),
                public,
            ],
            input=match.group(1),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        config = json.loads(output.read_text())
        assert config["version"] == app
        assert config["bundle"]["windows"]["wix"]["version"] == msi


@pytest.mark.parametrize(
    ("variant", "platform"), [("bundled", "Windows"), ("lite", "Windows"), ("bundled", "Linux")]
)
def test_actual_desktop_config_inherits_only_windows_pack_optimizations(
    tmp_path: Path, variant: str, platform: str
) -> None:
    """Run the shipping merge against a pinned UI overlay, retaining brand/signing policy."""
    import json
    import re

    steps = yaml.safe_load(_text(".github/workflows/clio-bundles.yml"))["jobs"]["desktop"]["steps"]
    build = next(step["run"] for step in steps if step.get("name") == "Tauri release build")
    match = re.search(r"<<'NODE'\n(.*?)\nNODE", build, flags=re.DOTALL)
    assert match is not None
    ui = tmp_path / "desktop/src-tauri"
    ui.mkdir(parents=True)
    (ui / "tauri.bundled.conf.json").write_text(
        json.dumps({"bundle": {"resources": ["gact-runtime/**/*"]}})
    )
    (ui / "tauri.bundled.windows-pack.conf.json").write_text(
        json.dumps(
            {
                "bundle": {
                    "resources": ["gact-runtime.tar.zst", "gact-runtime.pack.json"],
                    "windows": {"nsis": {"compression": "zlib"}},
                }
            }
        )
    )
    output = tmp_path / "config.json"
    result = subprocess.run(
        [
            "node",
            "-",
            variant,
            str(ROOT / "branding/clio/tauri.clio.conf.json"),
            str(output),
            "0.9.5-beta.5.2",
        ],
        input=match.group(1),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
        cwd=tmp_path,
        env={**os.environ, "RUNNER_OS": platform},
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(output.read_text())
    brand = json.loads(_text("branding/clio/tauri.clio.conf.json"))
    assert config["plugins"]["updater"]["pubkey"] == brand["plugins"]["updater"]["pubkey"]
    assert config["bundle"]["createUpdaterArtifacts"] == brand["bundle"]["createUpdaterArtifacts"]
    assert config["bundle"]["externalBin"] == brand["bundle"]["externalBin"]
    assert config["bundle"]["windows"]["wix"]["version"] == "0.9.5.5002"
    nsis = config["bundle"]["windows"]["nsis"]
    assert nsis["headerImage"] == brand["bundle"]["windows"]["nsis"]["headerImage"]
    assert nsis["sidebarImage"] == brand["bundle"]["windows"]["nsis"]["sidebarImage"]
    if variant == "bundled" and platform == "Windows":
        assert nsis["compression"] == "zlib"
        assert config["bundle"]["resources"] == ["gact-runtime.tar.zst", "gact-runtime.pack.json"]
    else:
        assert nsis.get("compression") == brand["bundle"]["windows"]["nsis"].get("compression")


def test_release_steps_after_the_check_use_the_package_version() -> None:
    """Only the tag check reads the tag; the rest use the package version it exports."""

    workflow = _text(".github/workflows/release.yml")
    assert workflow.count('"${GITHUB_REF_NAME#v}"') == 1
    assert workflow.count('version="$PKG_VERSION"') == 3
    assert "PKG_VERSION: ${{ needs.pypi.outputs.version }}" in workflow


def test_a_beta_tag_never_moves_the_latest_container_image() -> None:
    """ghcr `latest` follows stable tags only."""

    docker = _text(".github/workflows/docker.yml")
    workflow = yaml.safe_load(docker)
    metadata = next(step for step in workflow["jobs"]["build"]["steps"] if step.get("id") == "meta")
    # type=match adds latest independently of the explicit type=raw rule.
    assert metadata["with"]["flavor"] == "latest=false"
    assert (
        "type=raw,value=latest,enable=${{ startsWith(github.ref, 'refs/tags/v') "
        "&& !contains(github.ref_name, '-') }}"
    ) in docker


def test_container_channel_recovery_cannot_rebuild_versioned_images() -> None:
    """Explicit recovery writes the mutable channel without running the builder."""

    workflow = yaml.safe_load(_text(".github/workflows/docker.yml"))
    jobs = workflow["jobs"]
    assert jobs["build"]["if"] == "inputs.restore_stable_latest == ''"
    recovery = jobs["restore-stable-latest"]
    assert recovery["if"] == (
        "github.event_name == 'workflow_dispatch' && inputs.restore_stable_latest != ''"
    )
    assert recovery["permissions"] == {"contents": "read", "packages": "write"}
    assert not any("build-push-action" in step.get("uses", "") for step in recovery["steps"])
    for step in recovery["steps"]:
        if step.get("uses", "").startswith("docker/metadata-action@"):
            assert step["with"]["flavor"] == "latest=false"
    runner = recovery["steps"][-1]
    assert '--version "$STABLE_VERSION" --expected-latest "$EXPECTED_LATEST"' in runner["run"]
