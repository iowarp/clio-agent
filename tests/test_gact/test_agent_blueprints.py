from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy
import pytest
from fastapi.testclient import TestClient

from clio_agent import conf
from clio_agent.agent import ClioAgent
from clio_agent.gact import context as ctx
from clio_agent.gact.agent_blueprint_refresh import _default_blueprint_root_disabled
from clio_agent.gact.agent_blueprints import (
    DEFAULT_AGENT_BLUEPRINT_ID,
    DEFAULT_REGISTRY_COMMIT,
    DEFAULT_REGISTRY_REF,
    DEFAULT_REGISTRY_URL,
    default_registry_install_source,
    default_registry_metadata,
    default_registry_url,
    discover_agent_blueprints,
    ensure_default_registry_bootstrap,
    load_agent_blueprint_path,
    load_agent_blueprints,
    validate_agent_blueprint_path,
)
from clio_agent.gact.agents import toolset_inventory
from clio_agent.gact.app import (
    _active_base_agent_tool_executor,
    _blueprint_module_kind,
    _blueprint_runtime_signature,
    _build_blueprint_dspy_module,
    _builtin_agents,
    _dynamic_agent_tools,
    _extract_tools_called_from_trajectory,
    _gact_app_context,
    _gact_turn_timeout_s,
    _merge_tool_call_rows,
    _prediction_structured_metadata,
    _prediction_workflow_state,
    _recording_blueprint_tool,
    _run_blueprint_dspy_agent,
    _runtime_dynamic_agent_children_context,
    _tool_calls_from_handoff_rows,
    _user_agent_bool_param,
    _workflow_state_from_handoff_rows,
    _workflow_state_from_outputs,
    build_app,
)
from clio_agent.gact.runtime.globals import _UnsupportedSessionAgent
from clio_agent.gact.types import AgentDef
from tests.test_gact.conftest import complete_turn
from tests.test_gact.earthscope_schema import EARTHSCOPE_WORKFLOW_STATE_SCHEMA


def _fake_resolved_spec(provider: str, model: str) -> Any:
    """Stand in for a ``ResolvedLMSpec`` in tests that patch ``_dynamic_agent_lm_config``.

    The per-expert LM path (design §4) makes ``_dynamic_agent_lm_config`` return a
    ``ResolvedLMSpec`` whose ``materialize`` yields the runnable config. Tests that
    stub the resolver only care about the provider/model, so this returns a light
    object exposing the same ``materialize(cred_resolver) -> config`` contract.
    """

    def _materialize(cred_resolver: Any = None) -> Any:
        return SimpleNamespace(provider=provider, model=model)

    return SimpleNamespace(materialize=_materialize)


def _write_blueprint(root: Path, blueprint_id: str = "genomics") -> None:
    (root / "experts").mkdir(parents=True)
    root.joinpath("AGENT.md").write_text(
        f"""---
id: {blueprint_id}
version: 0.1.0
title: Genomics Agent
root_expert: root
defaults:
  prompt_profile: heavy
---
Genomics domain agent.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "root.md").write_text(
        """---
id: root
title: Genomics Root
tier: 1
module:
  kind: react
prompt_id: genomics.root
---
Coordinate genomics work.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "variant.md").write_text(
        """---
id: variant
title: Variant Expert
parent_id: root
tier: 2
tools:
  - memory_search_sessions
prompt_id: genomics.variant
---
Inspect variant evidence.
""",
        encoding="utf-8",
    )


def _write_data_root_blueprint(root: Path, blueprint_id: str = "remote-data") -> None:
    (root / "experts").mkdir(parents=True)
    root.joinpath("AGENT.md").write_text(
        f"""---
id: {blueprint_id}
version: 0.1.0
title: Remote Data Agent
default_expert: data
---
Remote marketplace agent.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "data.md").write_text(
        """---
id: data
title: Remote Data Orchestrator
tier: 1
prompt_profile: heavy
---
REMOTE BLUEPRINT ORCHESTRATOR MARKER.
""",
        encoding="utf-8",
    )


def _write_default_registry_blueprint(config_dir: Path) -> Path:
    # ``config_dir`` is the resolved per-user config root (the value
    # ``CLIO_USER_DIR`` resolves to for both ``user_config_dir`` and
    # ``user_config_dir_for``). Writing the blueprint under
    # ``<config_dir>/agent-blueprints/<id>`` mirrors the production install root
    # on every OS, so the test-written blueprint is the one discovered (the
    # Linux-only ``home/.config`` layout does not take effect on Windows).
    root = config_dir / "agent-blueprints" / DEFAULT_AGENT_BLUEPRINT_ID
    _write_blueprint(root, blueprint_id=DEFAULT_AGENT_BLUEPRINT_ID)
    root.joinpath(".clio-install.md").write_text(
        "\n".join(
            [
                "# CLIO Agent Blueprint install metadata",
                "",
                f"source: {DEFAULT_REGISTRY_URL}",
                "source_kind: git",
                f"ref: {DEFAULT_REGISTRY_REF}",
                f"commit: {DEFAULT_REGISTRY_COMMIT}",
                f"pinned_commit: {DEFAULT_REGISTRY_COMMIT}",
                "scope: global",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return root


def test_default_registry_agent_blueprint_is_discoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # Isolate the per-user config dir with the cross-OS ``CLIO_USER_DIR``
    # override; the injected ``home=``/XDG layout is Linux-only and would read
    # the developer's real store on Windows/macOS.
    user_dir = tmp_path / "user-config"
    monkeypatch.setenv("CLIO_USER_DIR", str(user_dir))
    _write_default_registry_blueprint(user_dir)
    blueprints = {
        row.id: row for row in discover_agent_blueprints(home=tmp_path, cwd=tmp_path / "workspace")
    }
    agents = {
        row.id: row
        for row in load_agent_blueprints(
            home=tmp_path,
            cwd=tmp_path / "workspace",
            blueprint_id=DEFAULT_AGENT_BLUEPRINT_ID,
        )
    }

    assert blueprints[DEFAULT_AGENT_BLUEPRINT_ID].scope == "global"
    assert blueprints[DEFAULT_AGENT_BLUEPRINT_ID].root_expert == "root"
    assert {"root", "variant"} <= set(agents)
    assert agents["variant"].metadata["agent_blueprint_id"] == DEFAULT_AGENT_BLUEPRINT_ID
    assert agents["variant"].metadata["agent_blueprint_scope"] == "global"
    assert "agent_blueprints/builtin" not in agents["variant"].metadata["definition_path"]
    assert (
        blueprints[DEFAULT_AGENT_BLUEPRINT_ID].metadata["install"]["commit"]
        == DEFAULT_REGISTRY_COMMIT
    )


def test_builtin_agents_never_implicitly_load_an_installed_default_registry_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELIBERATE FLIP (was ``test_builtin_agents_are_loaded_from_default_registry_snapshot``).

    ``_builtin_agents()`` used to silently load whatever Agent Blueprint snapshot
    was pinned as ``DEFAULT_AGENT_BLUEPRINT_ID`` and relabel its rows "builtin" --
    the same implicit-selection anti-pattern the blueprint lane's
    explicit-activation-only ruling (owner, 2026-08-05, commit aa906022) forbids
    for ``_runtime_active_agent_blueprint_id``, just surviving in this parallel
    "builtin catalog" seam. An installed-but-never-activated snapshot is now
    irrelevant to ``_builtin_agents()``: it always returns just the code-shipped
    react main (``catalog._builtin_main_agent``), regardless of what happens to
    be installed on disk.
    """

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # Isolate the per-user config dir with the cross-OS ``CLIO_USER_DIR``
    # override (Linux-only home/.config layout does not take effect on Windows).
    user_dir = tmp_path / "user-config"
    monkeypatch.setenv("CLIO_USER_DIR", str(user_dir))
    _write_default_registry_blueprint(user_dir)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    agents = {row.id: row for row in _builtin_agents()}

    assert set(agents) == {"main"}
    assert agents["main"].source == "builtin"
    assert agents["main"].metadata.get("definition_kind") == "builtin_main"
    assert "source_blueprint" not in agents["main"].metadata


def test_default_registry_url_default_is_https() -> None:
    """Regression (#764): the baked-in default must be a keyless https remote."""

    assert DEFAULT_REGISTRY_URL == "https://github.com/iowarp/clio-agent-marketplace.git"
    assert default_registry_url() == DEFAULT_REGISTRY_URL


def test_default_registry_url_env_override_selects_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (#764): CLIO_BLUEPRINT_REGISTRY_URL must drive URL selection."""

    override = "https://example.com/custom-marketplace.git"
    monkeypatch.setenv("CLIO_BLUEPRINT_REGISTRY_URL", override)
    assert default_registry_url() == override
    assert default_registry_metadata()["source"] == override
    # An explicit override wins even when a dev checkout carries the
    # marketplace submodule as a local install source.
    assert default_registry_install_source() == override


def test_default_registry_url_conf_file_wins_over_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (#764): file layer beats env, per conf.resolve precedence."""

    workspace = tmp_path / "workspace"
    (workspace / ".clio").mkdir(parents=True)
    (workspace / ".clio" / "config.yaml").write_text(
        "gact:\n  blueprint_registry:\n    url: https://example.com/file-marketplace.git\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("CLIO_BLUEPRINT_REGISTRY_URL", "https://example.com/env-marketplace.git")
    conf.reload()
    try:
        assert default_registry_url() == "https://example.com/file-marketplace.git"
    finally:
        conf.reload()


def test_default_registry_url_blank_configured_value_falls_back_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression (#764): a blank configured URL degrades loudly to the default."""

    workspace = tmp_path / "workspace"
    (workspace / ".clio").mkdir(parents=True)
    (workspace / ".clio" / "config.yaml").write_text(
        'gact:\n  blueprint_registry:\n    url: ""\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(workspace)
    conf.reload()
    try:
        with caplog.at_level("WARNING", logger="clio_agent.gact.agent_blueprints"):
            assert default_registry_url() == DEFAULT_REGISTRY_URL
        assert any(
            "blueprint_registry_url_fallback" in record.getMessage()
            and "blank_configured_value" in record.getMessage()
            for record in caplog.records
        )
    finally:
        conf.reload()


# --------------------------------------------------------------------------- #
# #948 S4b: upgrade-path refresh for a stale (pre-migration) default install.   #
# --------------------------------------------------------------------------- #

_STALE_MAIN_MD = """---
id: main
title: Main Agent
description: Pre-migration Tier-1 orchestrator.
tier: 1
module:
  kind: chain_of_thought
prompt_id: clio.main.planner
---
Pre-migration chain_of_thought orchestrator (cannot reach children).
"""

_STALE_CHILD_MD = """---
id: data
title: Data Expert
description: A declared child the chain_of_thought root cannot reach.
parent_id: main
tier: 2
module:
  kind: react
prompt_id: clio.expert.data
---
Data child.
"""

_REACT_MAIN_MD = """---
id: main
title: Main Agent
description: Migrated Tier-1 react orchestrator.
tier: 1
module:
  kind: react
prompt_id: clio.main.planner
---
Migrated react orchestrator (reaches children via spawn tools).
"""

_REACT_CHILD_MD = """---
id: data
title: Data Expert
description: A declared child reachable from the react root.
parent_id: main
tier: 2
module:
  kind: react
prompt_id: clio.expert.data
---
Data child.
"""

_DEFAULT_AGENT_MD = f"""---
id: {DEFAULT_AGENT_BLUEPRINT_ID}
version: 0.1.0
title: EarthScope GNSS Region
description: Default registry blueprint.
root_expert: main
---
Default registry Agent Blueprint.
"""


def _write_blueprint_tree(root: Path, *, main_md: str, child_md: str, commit: str) -> None:
    """Write a default-blueprint install tree (AGENT.md + experts + install meta)."""

    (root / "experts").mkdir(parents=True, exist_ok=True)
    root.joinpath("AGENT.md").write_text(_DEFAULT_AGENT_MD, encoding="utf-8")
    root.joinpath("experts", "main.md").write_text(main_md, encoding="utf-8")
    root.joinpath("experts", "data.md").write_text(child_md, encoding="utf-8")
    root.joinpath(".clio-install.md").write_text(
        "\n".join(
            [
                "# CLIO Agent Blueprint install metadata",
                "",
                f"source: {DEFAULT_REGISTRY_URL}",
                "source_kind: git",
                f"ref: {DEFAULT_REGISTRY_REF}",
                f"commit: {commit}",
                f"pinned_commit: {DEFAULT_REGISTRY_COMMIT}",
                "scope: global",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _make_migrated_registry_repo(path: Path) -> str:
    """Build a local git registry (branch ``main``) carrying the migrated packs.

    Returns a ``file://`` URL suitable for ``gact.blueprint_registry.url`` so the
    bootstrap refresh exercises the real clone path (not a direct-path copy).
    """

    path.mkdir(parents=True, exist_ok=True)
    _write_blueprint_tree(
        path, main_md=_REACT_MAIN_MD, child_md=_REACT_CHILD_MD, commit="migrated-head"
    )
    # The install-metadata file is a per-box install artifact, not registry
    # content; drop it from the source repo so a clone carries only the packs.
    path.joinpath(".clio-install.md").unlink()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "clio-test",
        "GIT_AUTHOR_EMAIL": "clio-test@example.com",
        "GIT_COMMITTER_NAME": "clio-test",
        "GIT_COMMITTER_EMAIL": "clio-test@example.com",
    }
    subprocess.run(
        ["git", "init", "-b", "main", str(path)], check=True, env=env, capture_output=True
    )
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, env=env, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "migrated react packs"],
        check=True,
        env=env,
        capture_output=True,
    )
    return path.as_uri()


def _prepare_default_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    registry_url: str = "",
) -> tuple[Path, Path]:
    """Point the per-user store at a fresh tmp dir with bootstrap ENABLED.

    Returns ``(install_root_for_default_blueprint, home)``. The default blueprint
    install dir is left empty for callers to populate.
    """

    store = tmp_path / "store"
    # CLIO_USER_DIR is the ONLY cross-OS isolation: platformdirs ignores
    # XDG_CONFIG_HOME on Windows, so an XDG-only override silently pointed the
    # per-user store (and the blueprint-sources registry) at the REAL
    # %LOCALAPPDATA% — every test run leaked a pytest-tmpdir source row into
    # the production registry (~100 dead genomics rows found 2026-08-13).
    config_dir = store / "clio-agent"
    monkeypatch.setenv("CLIO_USER_DIR", str(config_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(store))
    # #948 S4b: the refresh path must actually run, so re-enable the bootstrap
    # (conftest disables it globally for unit isolation).
    monkeypatch.delenv("CLIO_AGENT_DISABLE_DEFAULT_REGISTRY_BOOTSTRAP", raising=False)
    if registry_url:
        # Config-over-env (the config-over-env principle): drive the registry URL
        # through the config FILE, not CLIO_BLUEPRINT_REGISTRY_URL.
        config_dir.mkdir(parents=True, exist_ok=True)
        config_dir.joinpath("config.yaml").write_text(
            f"gact:\n  blueprint_registry:\n    url: {registry_url}\n",
            encoding="utf-8",
        )
    conf.reload()
    install_root = config_dir / "agent-blueprints" / DEFAULT_AGENT_BLUEPRINT_ID
    return install_root, tmp_path / "home"


def test_stale_default_install_is_refreshed_from_migrated_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(a) A pre-migration chain_of_thought root refreshes to a react root."""

    registry_url = _make_migrated_registry_repo(tmp_path / "registry")
    install_root, home = _prepare_default_store(tmp_path, monkeypatch, registry_url=registry_url)
    _write_blueprint_tree(
        install_root, main_md=_STALE_MAIN_MD, child_md=_STALE_CHILD_MD, commit="stale-head"
    )
    try:
        # Precondition: the installed root is disabled by validation (dead end).
        disabled_before, errors_before = _default_blueprint_root_disabled(install_root)
        assert disabled_before, "stale chain_of_thought root must start disabled"
        assert any("cannot reach them" in err for err in errors_before)

        with caplog.at_level("WARNING", logger="clio_agent.gact.agent_blueprints"):
            diagnostic = ensure_default_registry_bootstrap(home=home, cwd=tmp_path / "cwd")

        # The refresh repaired the box: no diagnostic, root now enabled + react.
        assert diagnostic == ""
        disabled_after, _ = _default_blueprint_root_disabled(install_root)
        assert not disabled_after, "refreshed react root must validate enabled"
        validation = validate_agent_blueprint_path(install_root, scope="global")
        main_row = next(row for row in validation["agents"] if row["id"] == "main")
        assert main_row["enabled"] is True
        assert main_row["module"]["kind"] == "react"
        # Sabotage guard: the on-disk root file was actually replaced.
        assert "chain_of_thought" not in install_root.joinpath("experts", "main.md").read_text(
            encoding="utf-8"
        )
        # Structured migration reason reached the log with old + new commit.
        messages = [rec.getMessage() for rec in caplog.records]
        assert any(
            "default_registry_refreshed reason=root_disabled_stale_install" in msg
            and "old_commit=stale-head" in msg
            and "new_commit=" in msg
            for msg in messages
        ), messages
    finally:
        conf.reload()


def test_stale_default_install_kept_when_refresh_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(b) An unreachable registry keeps the stale install (never deletes it)."""

    missing = (tmp_path / "no-such-registry").as_uri()
    install_root, home = _prepare_default_store(tmp_path, monkeypatch, registry_url=missing)
    _write_blueprint_tree(
        install_root, main_md=_STALE_MAIN_MD, child_md=_STALE_CHILD_MD, commit="stale-head"
    )
    original_agent_md = install_root.joinpath("AGENT.md").read_bytes()
    original_main_md = install_root.joinpath("experts", "main.md").read_bytes()
    try:
        with caplog.at_level("WARNING", logger="clio_agent.gact.agent_blueprints"):
            diagnostic = ensure_default_registry_bootstrap(home=home, cwd=tmp_path / "cwd")

        # Failure is surfaced, not swallowed.
        assert "stale install kept" in diagnostic
        assert any(
            "default_registry_refresh_failed reason=root_disabled_stale_install" in rec.getMessage()
            for rec in caplog.records
        )
        # The only copy was NOT deleted and NOT mutated.
        assert install_root.joinpath("AGENT.md").read_bytes() == original_agent_md
        assert install_root.joinpath("experts", "main.md").read_bytes() == original_main_md
        disabled_after, _ = _default_blueprint_root_disabled(install_root)
        assert disabled_after, "stale install must remain (still disabled) after a failed refresh"
    finally:
        conf.reload()


def test_valid_default_install_is_not_refreshed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) A valid react install triggers no refresh (no churn).

    Hermetic registry: an explicit LOCAL registry override carrying only the
    already-installed default pack — without it the source resolves to the real
    dev submodule and the per-boot sync legitimately installs the other
    marketplace packs, which this test's no-churn spy must not conflate with a
    refresh (review 2026-08-13 finding #10).
    """

    local_registry = tmp_path / "registry-local"
    default_pack = local_registry / DEFAULT_AGENT_BLUEPRINT_ID
    _write_blueprint_tree(
        default_pack, main_md=_REACT_MAIN_MD, child_md=_REACT_CHILD_MD, commit="valid-head"
    )
    default_pack.joinpath(".clio-install.md").unlink()
    install_root, home = _prepare_default_store(
        tmp_path, monkeypatch, registry_url=local_registry.as_posix()
    )
    _write_blueprint_tree(
        install_root, main_md=_REACT_MAIN_MD, child_md=_REACT_CHILD_MD, commit="valid-head"
    )
    install_meta = install_root / ".clio-install.md"
    before_mtime = install_meta.stat().st_mtime_ns
    before_bytes = install_meta.read_bytes()

    calls: list[str] = []

    def _spy_install(*_args: object, **_kwargs: object) -> dict[str, Any]:
        calls.append("install")
        raise AssertionError("install_agent_blueprint must not run for a valid install")

    # The refresh path calls install through the owner module's binding, so spy
    # there (a valid install must trigger neither a refresh nor a fresh install).
    monkeypatch.setattr(
        "clio_agent.gact.agent_blueprint_refresh.install_agent_blueprint", _spy_install
    )
    try:
        diagnostic = ensure_default_registry_bootstrap(home=home, cwd=tmp_path / "cwd")
    finally:
        conf.reload()

    assert diagnostic == ""
    assert calls == [], "no refresh (and no fresh install) may run for a valid install"
    assert install_meta.stat().st_mtime_ns == before_mtime
    assert install_meta.read_bytes() == before_bytes


def test_workflow_state_normalizes_unicode_hyphens_in_path_fields() -> None:
    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "local_path": "/tmp/.clio/artifacts/ndp\u2011staging/MTA1.csv",
                            "source_url": "https://example.test/raw_csv/MTA1.csv",
                        },
                        "artifact": {
                            "status": "ready",
                            "path": "/tmp/.clio/artifacts/plots/MTA1\u2011plot.png",
                        },
                    }
                }
            )
        ],
        schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA,
    )

    assert state["acquisition"]["local_path"] == "/tmp/.clio/artifacts/ndp-staging/MTA1.csv"
    assert state["artifact"]["path"] == "/tmp/.clio/artifacts/plots/MTA1-plot.png"


def test_agent_blueprint_respects_boolean_enabled_false(tmp_path: Path) -> None:
    blueprint = tmp_path / "disabled-agent"
    _write_blueprint(blueprint, blueprint_id="disabled-agent")
    blueprint.joinpath("experts", "variant.md").write_text(
        """---
id: variant
title: Disabled Variant
parent_id: root
tier: 2
enabled: false
---
This expert is intentionally disabled.
""",
        encoding="utf-8",
    )

    rows = {row.id: row for row in load_agent_blueprint_path(blueprint)}

    assert rows["root"].enabled is True
    assert rows["variant"].enabled is False


def test_agent_blueprint_module_kind_and_structured_outputs_parse(tmp_path: Path) -> None:
    root = tmp_path / "react-blueprint"
    _write_blueprint(root, blueprint_id="react-blueprint")
    root.joinpath("experts", "root.md").write_text(
        """---
id: root
title: React Root
tier: 1
module:
  kind: react
  max_iters: 3
signature:
  inputs: question
  outputs: answer
structured_outputs:
  evidence: true
  artifacts: true
fanout:
  max_workers: 2
tools:
  - memory_search_sessions
---
Coordinate with ReAct.
""",
        encoding="utf-8",
    )

    rows = {row.id: row for row in load_agent_blueprint_path(root)}
    assert rows["root"].module["kind"] == "react"
    assert rows["root"].module["max_iters"] == 3
    assert rows["root"].signature["inputs"] == "question"
    assert rows["root"].structured_outputs["evidence"] is True
    assert rows["root"].fanout["max_workers"] == 2


def test_agent_blueprint_loader_expands_pack_local_includes(tmp_path: Path) -> None:
    root = tmp_path / "included-blueprint"
    _write_blueprint(root, blueprint_id="included-blueprint")
    root.joinpath("AGENT.md").write_text(
        """---
id: included-blueprint
version: 0.1.0
title: Included Blueprint
root_expert: root
experts:
  - experts/root.md
includes:
  - modules/ndp-collector/experts
---
Agent with included module experts.
""",
        encoding="utf-8",
    )
    included = root / "modules" / "ndp-collector" / "experts"
    included.mkdir(parents=True)
    included.joinpath("ndp_catalog.md").write_text(
        """---
id: ndp_catalog
title: NDP Catalog
parent_id: variant
tier: 3
module_kind: react
tools:
  - ndp_search_datasets
---
Search NDP datasets.
""",
        encoding="utf-8",
    )

    rows = {row.id: row for row in load_agent_blueprint_path(root)}

    assert "ndp_catalog" in rows
    assert rows["ndp_catalog"].parent_id == "variant"
    assert rows["ndp_catalog"].metadata["definition_kind"] == "agent_blueprint"
    assert (
        "modules/ndp-collector/experts/ndp_catalog.md"
        in rows["ndp_catalog"].metadata["definition_path"]
    )


def test_blueprint_runtime_signature_preserves_fields_and_normalizes_structured_outputs() -> None:
    agent_def = AgentDef(
        id="semantic-root",
        source="expert_pack",
        title="Semantic Root",
        signature={
            "inputs": {
                "question": "User request",
                "dataset_summary": "Available dataset summary",
            },
            "outputs": {
                "answer": "User-facing answer",
                "artifact_plan": "Planned artifact work",
            },
        },
        structured_outputs={
            "evidence": "true",
            "artifacts": "false",
            "errors": True,
            "delegation": False,
        },
    )

    signature = _blueprint_runtime_signature(agent_def)

    # ``system_prompt`` is always carried as an input so the expert's built body
    # reaches the model (see _blueprint_runtime_signature). Declared inputs follow.
    assert list(signature.input_fields) == ["system_prompt", "question", "dataset_summary"]
    # CLEAN CONTRACT: the only auto-injected structured output is ``workflow_state``
    # (default-enabled). #948 S4 removed the settle-loop routing fields — an
    # orchestrator now routes by CALLING its spawn tools, so the signature carries no
    # routing field. The legacy companions (evidence/artifacts/errors/delegation/
    # expert_handoffs) are no longer injected regardless of structured_outputs.
    assert list(signature.output_fields) == [
        "answer",
        "artifact_plan",
        "workflow_state",
    ]
    for legacy in ("evidence", "artifacts", "errors", "delegation", "expert_handoffs"):
        assert legacy not in signature.output_fields


def test_blueprint_runtime_signature_preserves_declared_field_types() -> None:
    signature = _blueprint_runtime_signature(
        AgentDef(
            id="typed",
            source="expert_pack",
            title="Typed",
            signature={
                "inputs": {
                    "question": {"description": "User request", "type": "string"},
                    "limit": {"description": "Maximum rows", "type": "integer"},
                    "bbox": {"description": "GeoJSON-like bbox", "type": "array"},
                },
                "outputs": [
                    {"name": "answer", "description": "Final answer", "type": "str"},
                    {"name": "score", "description": "Quality score", "type": "float"},
                    {"name": "metadata", "description": "Structured metadata", "type": "dict"},
                    {"name": "needs_review", "description": "Review flag", "type": "bool"},
                ],
            },
            structured_outputs={
                "workflow_state": False,
                "evidence": False,
                "artifacts": False,
                "errors": False,
                "delegation": False,
                "expert_handoffs": False,
            },
        )
    )

    assert signature.__annotations__["question"] is str
    assert signature.__annotations__["limit"] is int
    assert signature.__annotations__["bbox"] is list
    assert signature.__annotations__["score"] is float
    assert signature.__annotations__["metadata"] is dict
    assert signature.__annotations__["needs_review"] is bool


def test_blueprint_runtime_signature_defaults_empty_declarations_to_question_and_answer() -> None:
    signature = _blueprint_runtime_signature(
        AgentDef(id="data", source="expert_pack", title="Data")
    )

    assert list(signature.input_fields) == ["system_prompt", "question"]
    assert list(signature.output_fields) == [
        "answer",
        "workflow_state",
    ]


@pytest.mark.parametrize(
    ("raw_signature", "expected_inputs", "expected_outputs"),
    [
        ({}, ["system_prompt", "question"], ["answer"]),
        ({"inputs": {}, "outputs": {}}, ["system_prompt", "question"], ["answer"]),
        ({"inputs": [], "outputs": []}, ["system_prompt", "question"], ["answer"]),
        ({"outputs": {"summary": "Short summary"}}, ["system_prompt", "question"], ["summary"]),
        (
            # declared inputs without system_prompt → it is prepended
            {"inputs": ["question", "file_context"], "outputs": ["answer", "quality_flags"]},
            ["system_prompt", "question", "file_context"],
            ["answer", "quality_flags"],
        ),
        (
            {
                "input": [{"name": "question", "description": "User goal"}],
                "output": [{"id": "answer", "desc": "Final answer"}],
            },
            ["system_prompt", "question"],
            ["answer"],
        ),
    ],
)
def test_blueprint_runtime_signature_field_declaration_matrix(
    raw_signature: dict[str, Any],
    expected_inputs: list[str],
    expected_outputs: list[str],
) -> None:
    signature = _blueprint_runtime_signature(
        AgentDef(
            id="matrix",
            source="expert_pack",
            title="Matrix",
            signature=raw_signature,
            structured_outputs={
                "workflow_state": False,
                "evidence": False,
                "artifacts": False,
                "errors": False,
                "delegation": False,
                "expert_handoffs": False,
            },
        )
    )

    assert list(signature.input_fields) == expected_inputs
    # workflow_state is disabled here and #948 S4 removed the settle-loop routing
    # fields, so the outputs are exactly the declared ones.
    assert list(signature.output_fields) == expected_outputs


@pytest.mark.parametrize(
    ("value", "enabled"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("disabled", False),
        ("yes", True),
    ],
)
def test_blueprint_structured_output_enablement_matrix(value: Any, enabled: bool) -> None:
    # ``workflow_state`` is the only auto-injected structured output, and its
    # ``structured_outputs`` toggle accepts bools and truthy/falsey strings.
    signature = _blueprint_runtime_signature(
        AgentDef(
            id="structured",
            source="expert_pack",
            title="Structured",
            structured_outputs={"workflow_state": value},
        )
    )

    assert ("workflow_state" in signature.output_fields) is enabled


def test_blueprint_module_kind_rejects_unsupported_values() -> None:
    with pytest.raises(ValueError, match="unsupported module.kind"):
        _blueprint_module_kind(
            AgentDef(
                id="bad",
                source="expert_pack",
                title="Bad",
                module={"kind": "native_python"},
            )
        )


def test_gact_turn_timeout_default_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIO_GACT_TURN_TIMEOUT_S", raising=False)
    assert _gact_turn_timeout_s() == 900.0

    monkeypatch.setenv("CLIO_GACT_TURN_TIMEOUT_S", "0.2")
    assert _gact_turn_timeout_s() == 0.2

    monkeypatch.setenv("CLIO_GACT_TURN_TIMEOUT_S", "not-a-number")
    assert _gact_turn_timeout_s() == 900.0


def test_user_agent_bool_param_parses_depth_chain_opt_in() -> None:
    assert (
        _user_agent_bool_param(
            AgentDef(
                id="depth",
                source="expert_pack",
                title="Depth",
                parameters={"bubble_child_evidence_on_completion": "true"},
            ),
            "bubble_child_evidence_on_completion",
        )
        is True
    )
    assert (
        _user_agent_bool_param(
            AgentDef(
                id="width",
                source="expert_pack",
                title="Width",
                parameters={"bubble_child_evidence_on_completion": "false"},
            ),
            "bubble_child_evidence_on_completion",
            default=True,
        )
        is False
    )
    assert (
        _user_agent_bool_param(
            AgentDef(id="default", source="expert_pack", title="Default"),
            "bubble_child_evidence_on_completion",
        )
        is False
    )


def test_workflow_state_merge_preserves_staged_acquisition_over_metadata_only(
    tmp_path: Path,
) -> None:
    staged_csv = tmp_path / "changed_station.csv"
    staged_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")
    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "resource_candidate": {"status": "metadata_only"},
                        "acquisition": {
                            "status": "metadata_only",
                            "analysis_ready": False,
                            "metadata_path": "/workspace/earthscope_converted_data.csv",
                        },
                    }
                }
            ),
            json.dumps(
                {
                    "workflow_state": {
                        "resource_candidate": {
                            "status": "selected",
                            "resource_name": "changed_station.csv",
                        },
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "local_path": str(staged_csv),
                        },
                    }
                }
            ),
            json.dumps(
                {
                    "workflow_state": {
                        "resource_candidate": {"status": "metadata_only"},
                        "acquisition": {
                            "status": "metadata_only",
                            "analysis_ready": False,
                            "metadata_path": "/workspace/old_metadata.csv",
                        },
                    }
                }
            ),
        ],
        schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA,
    )

    assert state["resource_candidate"]["status"] == "selected"
    assert state["resource_candidate"]["resource_name"] == "changed_station.csv"
    assert state["acquisition"]["status"] == "staged"
    assert state["acquisition"]["analysis_ready"] is True
    assert state["acquisition"]["local_path"] == str(staged_csv)


def test_workflow_state_merge_preserves_non_empty_tool_provenance(tmp_path: Path) -> None:
    staged_csv = tmp_path / "MTA1.CI.LY_.30.csv"
    staged_csv.write_text("time,east,north,up\n2026-01-01,0,0,0\n", encoding="utf-8")
    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "resource_candidate": {
                            "status": "selected",
                            "dataset_id": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                            "dataset_name": "mta1-ci-ly-30",
                            "resource_name": "MTA1.CI.LY_.30.csv",
                            "resource_url": "https://ds2.example.test/raw_csv/MTA1.CI.LY_.30.csv",
                        },
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "local_path": str(staged_csv),
                            "source_url": "https://ds2.example.test/raw_csv/MTA1.CI.LY_.30.csv",
                        },
                    }
                }
            ),
            json.dumps(
                {
                    "workflow_state": {
                        "resource_candidate": {
                            "status": "selected",
                            "dataset_id": "",
                            "dataset_name": "",
                            "resource_name": "MTA1.CI.LY_.30.csv",
                            "resource_url": "",
                        },
                        "acquisition": {
                            "status": "staged",
                            "analysis_ready": True,
                            "local_path": str(staged_csv),
                            "source_url": "",
                        },
                    }
                }
            ),
        ],
        schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA,
    )

    assert state["resource_candidate"]["dataset_id"] == "1b0c1b93-f164-4025-bd7b-000252b5ca18"
    assert (
        state["resource_candidate"]["resource_url"]
        == "https://ds2.example.test/raw_csv/MTA1.CI.LY_.30.csv"
    )
    assert (
        state["acquisition"]["source_url"] == "https://ds2.example.test/raw_csv/MTA1.CI.LY_.30.csv"
    )


def test_workflow_state_extraction_preserves_nested_child_structured_evidence() -> None:
    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "structured": {
                        "evidence": json.dumps(
                            {
                                "workflow_state": {
                                    "profile": {"status": "complete", "rows_scanned": 5000}
                                }
                            }
                        )
                    }
                }
            )
        ],
        schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA,
    )

    assert state["profile"]["status"] == "complete"
    assert state["profile"]["rows_scanned"] == 5000


def test_workflow_state_downgrades_analysis_ready_without_staged_local_path() -> None:
    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "acquisition": {
                            "status": "ready",
                            "analysis_ready": True,
                            "source_url": "https://example.test/raw_csv/WXYZ.csv",
                        },
                        "resource_candidate": {
                            "status": "metadata_confirmed",
                            "resource_urls": ["https://example.test/raw_csv/WXYZ.csv"],
                        },
                    }
                }
            )
        ],
        schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA,
    )

    assert state["acquisition"]["status"] == "candidate_found"
    assert state["acquisition"]["analysis_ready"] is False
    assert "staged local CSV path" in state["acquisition"]["blocker"]
    assert state["resource_candidate"]["resource_urls"] == ["https://example.test/raw_csv/WXYZ.csv"]


def test_workflow_state_reclassifies_data_available_without_staged_local_path() -> None:
    state = _workflow_state_from_outputs(
        [
            json.dumps(
                {
                    "workflow_state": {
                        "acquisition": {
                            "status": "data_available",
                            "analysis_ready": True,
                            "resource_urls": ["https://example.test/raw_csv/EFGH.CI.LY_.30.csv"],
                        },
                        "resource_candidate": {
                            "status": "ready",
                            "dataset_id": "changed-dataset",
                            "resource_name": "EFGH.CI.LY_.30.csv",
                            "resource_url": "https://example.test/raw_csv/EFGH.CI.LY_.30.csv",
                        },
                    }
                }
            )
        ],
        schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA,
    )

    assert state["acquisition"]["status"] == "candidate_found"
    assert state["acquisition"]["analysis_ready"] is False
    assert "staged local CSV path" in state["acquisition"]["blocker"]
    assert state["resource_candidate"]["status"] == "ready"


def test_blueprint_runner_uses_dspy_module_call_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class FakeBlueprintModule:
        def __call__(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return SimpleNamespace(answer="called")

        def forward(self, **kwargs: Any) -> Any:
            raise AssertionError("blueprint runner must use the DSPy module call path")

    monkeypatch.setattr(
        "clio_agent.gact.app._build_blueprint_dspy_module",
        lambda base_agent, agent_def: FakeBlueprintModule(),
    )

    result = _run_blueprint_dspy_agent(
        SimpleNamespace(),
        AgentDef(id="data", source="expert_pack", title="Data"),
        "prove call path",
        "sess_test",
    )

    assert result.answer == "called"
    assert calls == [
        {
            "question": "prove call path",
            "session_id": "sess_test",
            "cancel_requested": None,
        }
    ]


def test_blueprint_module_empty_answer_raises_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #948 S4: the settle/synthesis layer that once tolerated an empty root answer
    # is deleted. An empty answer is now a typed failure -- the module raises so the
    # turn records ``agent_error`` (turn.py) instead of a silent empty deliverable.
    class FakeProgram:
        def __call__(self, **kwargs: Any) -> Any:
            return SimpleNamespace(answer="", expert_handoffs="")

    class FakePredict:
        def __init__(self, signature: Any) -> None:
            self.signature = signature

        def __call__(self, **kwargs: Any) -> Any:
            return FakeProgram()(**kwargs)

    monkeypatch.setattr(dspy, "Predict", FakePredict)
    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object())
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda config: object())
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._dynamic_agent_lm_config",
        lambda base_agent, agent_def: _fake_resolved_spec("argonne", "gpt-oss-120b"),
    )

    module = _build_blueprint_dspy_module(
        SimpleNamespace(),
        AgentDef(id="main", source="expert_pack", title="Main", module={"kind": "predict"}),
    )

    with pytest.raises(RuntimeError, match="returned an empty answer"):
        module(question="deliver", session_id="session-123")


def test_blueprint_module_empty_answer_with_handoffs_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The retired "handoff-only root output" shape (empty answer + handoff rows) no
    # longer excuses an empty deliverable: routing is via the spawn-runtime tools,
    # so an empty answer raises regardless of any residual expert_handoffs value.
    class FakeProgram:
        def __call__(self, **kwargs: Any) -> Any:
            return SimpleNamespace(
                answer="",
                expert_handoffs='[{"agent_id":"reference","parent_id":"main","task":"inspect fasta"}]',
            )

    class FakePredict:
        def __init__(self, signature: Any) -> None:
            self.signature = signature

        def __call__(self, **kwargs: Any) -> Any:
            return FakeProgram()(**kwargs)

    monkeypatch.setattr(dspy, "Predict", FakePredict)
    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object())
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda config: object())
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._dynamic_agent_lm_config",
        lambda base_agent, agent_def: _fake_resolved_spec("argonne", "gpt-oss-120b"),
    )

    module = _build_blueprint_dspy_module(
        SimpleNamespace(),
        AgentDef(id="main", source="expert_pack", title="Main", module={"kind": "predict"}),
    )

    with pytest.raises(RuntimeError, match="returned an empty answer"):
        module(question="delegate", session_id="session-123")


def test_extract_tools_called_from_indexed_react_trajectory() -> None:
    rows = _extract_tools_called_from_trajectory(
        {
            "step_0_tool_name": "ndp_get_dataset_details",
            "step_0_tool_args": {
                "dataset_identifier": "811f0bcc-99e5-455c-bcf6-7c63c2634f41",
                "server": "global",
            },
            "step_0_observation": {
                "resources": [
                    {
                        "name": "earthscope_converted_data.csv",
                        "url": "https://example.test/earthscope_converted_data.csv",
                    }
                ]
            },
        }
    )

    assert rows == [
        {
            "name": "ndp_get_dataset_details",
            "args": {
                "dataset_identifier": "811f0bcc-99e5-455c-bcf6-7c63c2634f41",
                "server": "global",
            },
            "result": {
                "resources": [
                    {
                        "name": "earthscope_converted_data.csv",
                        "url": "https://example.test/earthscope_converted_data.csv",
                    }
                ]
            },
            "ok": True,
            "telemetry_source": "agent_trajectory",
        }
    ]


def test_merge_tool_call_rows_deduplicates_matching_call_id_with_result_evidence() -> None:
    rows = _merge_tool_call_rows(
        [
            {
                "call_id": "call_same",
                "name": "ndp_search_datasets",
                "args": {"search_terms": ["UCSF"]},
                "ok": True,
                "result": {"datasets": []},
                "telemetry_source": "live_observer",
            }
        ],
        [
            {
                "call_id": "call_same",
                "name": "ndp_search_datasets",
                "args": {"search_terms": ["UCSF"]},
                "ok": True,
                "result": {"datasets": []},
                "telemetry_source": "child_handoff",
            }
        ],
    )

    assert len(rows) == 1
    assert rows[0]["call_id"] == "call_same"


def test_completed_child_state_is_structural_not_prose_in_continuation() -> None:
    # End-to-end answer-cleanliness invariant for UI issue #1: after a child
    # completes with a non-empty typed workflow_state, (a) the user-/parent-facing
    # OUTPUT text carries NO "typed workflow state" prose block, yet (b) the
    # parent's continuation context still carries that state STRUCTURALLY.
    child_state = {
        "acquisition": {
            "status": "staged",
            "analysis_ready": True,
            "local_path": "/workspace/.clio/artifacts/P472.CI.LY_.20.csv",
        },
        "station_catalog": {"station_ids": ["P472", "SIO5"]},
    }
    # The child's GENUINE deliverable is clean human prose, flowed verbatim -- the
    # carrier of the typed state is the completed row's structured
    # ``workflow_state`` field.
    clean_answer = "Staged GNSS CSV for station P472 near the requested center."
    output = clean_answer
    completed_row = {
        "agent_id": "earthscope_station_catalog",
        "stage": "delegate.completed",
        "status": "completed",
        "output": output,
        "workflow_state": child_state,
    }

    # (a) No prose state block in the user-/parent-facing output text.
    assert "typed workflow state" not in output.casefold()
    assert "CLIO" not in output
    assert (
        _workflow_state_from_outputs([clean_answer], schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA) == {}
    )

    # (b1) The structural carrier (the row's workflow_state field) holds the state,
    # readable by the parent's handoff-row reader.
    recovered = _workflow_state_from_handoff_rows(
        [completed_row], schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA
    )
    assert recovered["acquisition"]["status"] == "staged"
    assert recovered["station_catalog"]["station_ids"] == ["P472", "SIO5"]


def test_nested_handoff_tool_calls_preserve_child_result_evidence() -> None:
    rows = _tool_calls_from_handoff_rows(
        [
            {
                "agent_id": "main",
                "children": [
                    {
                        "agent_id": "ndp_resource_resolver",
                        "tools_called": [
                            {
                                "name": "ndp_stage_resource",
                                "args": {
                                    "dataset_identifier": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                                    "resource_name": "MTA1.CI.LY_.30.csv",
                                },
                                "result": {
                                    "path": "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv",
                                    "resource_name": "MTA1.CI.LY_.30.csv",
                                },
                                "ok": True,
                                "telemetry_source": "agent_trajectory",
                            }
                        ],
                    }
                ],
            }
        ]
    )

    assert rows == [
        {
            "name": "ndp_stage_resource",
            "args": {
                "dataset_identifier": "1b0c1b93-f164-4025-bd7b-000252b5ca18",
                "resource_name": "MTA1.CI.LY_.30.csv",
            },
            "ok": True,
            "telemetry_source": "agent_trajectory",
            "result": {
                "path": "/workspace/.clio/artifacts/ndp-staging/MTA1.CI.LY_.30.csv",
                "resource_name": "MTA1.CI.LY_.30.csv",
            },
        }
    ]


def test_recording_blueprint_tool_captures_context_local_tool_result() -> None:
    def sample_tool(station: str) -> dict[str, Any]:
        return {"station": station, "ok": True}

    tool = dspy.Tool(
        func=sample_tool,
        name="sample_station_tool",
        desc="Sample station tool",
        args={"station": {"type": "string"}},
    )
    rows: list[dict[str, Any]] = []
    token = ctx.set_blueprint_tool_rows(rows)
    try:
        wrapped = _recording_blueprint_tool(tool)
        result = wrapped(station="UCSF")
    finally:
        ctx.reset(token)

    assert result == {"station": "UCSF", "ok": True}
    assert rows == [
        {
            "name": "sample_station_tool",
            "args": {"station": "UCSF"},
            "ok": True,
            "duration_ms": pytest.approx(rows[0]["duration_ms"]),
            "result": {"station": "UCSF", "ok": True},
            "telemetry_source": "blueprint_react_tool_wrapper",
        }
    ]


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_station_catalog_prompt_keeps_resolver_acquisition_boundary(
    blueprint_id: str,
) -> None:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "clio-agent-marketplace"
        / blueprint_id
        / "experts"
        / "earthscope_station_catalog.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    tool_block = prompt.split("---", 2)[1]

    assert "station metadata ranking, not station time-series acquisition" in prompt
    assert "It has no NDP search or staging tools" in prompt
    # clio-kit renamed this tool: ndp_filter_earthscope_station_catalog -> geo_filter_points_by_radius
    assert "  - geo_filter_points_by_radius" in tool_block
    assert "  - ndp_search_datasets" not in tool_block
    assert "  - ndp_stage_resource" not in tool_block
    assert "Do not call `ndp_stage_resource` for a station-specific time-series CSV" in prompt
    assert (
        "do not call `ndp_search_datasets` to search station-specific resources by station ID"
        in prompt
    )
    assert "The `ndp_resource_resolver` expert owns station-specific resource search" in prompt
    assert "resource_discovery.station_resource_queries" in prompt


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_analysis_prompt_forbids_rows_scanned_cadence_inference(
    blueprint_id: str,
) -> None:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "clio-agent-marketplace"
        / blueprint_id
        / "experts"
        / "gnss_timeseries_analysis.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    normalized_prompt = " ".join(prompt.split())

    assert (
        "Never convert `rows_scanned` into duration, cadence, or a sampling rate"
        in normalized_prompt
    )
    assert (
        "`rows_scanned`, `rows_examined`, and file size are profiler coverage signals"
        in normalized_prompt
    )
    assert "Treat `numeric_summary_rows` or" in prompt
    assert 'Do not infer a "30-day record" from `.30`' in normalized_prompt
    assert "visible sample rows suggest that local spacing" in normalized_prompt
    assert "do not generalize" in prompt
    assert "file-wide `Hz`, days-long duration, or sampling-rate claim" in normalized_prompt


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_station_network_prompt_preserves_uncertainty_units(
    blueprint_id: str,
) -> None:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "clio-agent-marketplace"
        / blueprint_id
        / "experts"
        / "station_network_analysis.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")

    assert "Values such as `0.033 m` are centimeter-scale" in prompt
    assert "not sub-centimeter" in prompt
    assert 'Do not call uncertainty "sub-cm" unless the' in prompt
    assert "If the evidence is scan-limited" in prompt


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_station_network_prompt_forbids_scan_limited_record_claims(
    blueprint_id: str,
) -> None:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "clio-agent-marketplace"
        / blueprint_id
        / "experts"
        / "station_network_analysis.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    normalized_prompt = " ".join(prompt.split())

    assert "Do not infer cadence, duration, complete coverage, or gap-free behavior" in prompt
    assert "`rows_scanned`, `rows_examined`, `rows_profiled`" in prompt
    assert "resource names, or adjacent sample rows" in prompt
    assert '"30-day record", "30 s cadence"' in prompt
    assert '"30-day record", "30 s cadence", "two-week record"' in normalized_prompt
    assert '"full record", "continuous", "no large data gaps"' in normalized_prompt
    assert "full-file cadence/duration/gap quality was not verified" in normalized_prompt
    assert 'Prefer wording such as "preliminary station/resource' in normalized_prompt
    assert "Treat `qChannel` as an opaque numeric flag" in prompt
    assert "`missing_values_scope=profiled_rows`" in prompt


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_analysis_prompt_filters_child_scan_limited_record_claims(
    blueprint_id: str,
) -> None:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "clio-agent-marketplace"
        / blueprint_id
        / "experts"
        / "analysis.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    normalized_prompt = " ".join(prompt.split())

    assert "audit child summaries for unsupported" in prompt
    assert '"30-day' in prompt
    assert '"30 s cadence", "two-week record"' in prompt
    assert '"full record", "continuous", "no large data gaps"' in normalized_prompt
    assert '"high' in prompt and '"excellent coverage"' in prompt
    assert "`rows_scanned`/`rows_examined`" in prompt
    assert "`rows_profiled`/`numeric_summary_rows`" in prompt
    assert "omit that phrase from the returned" in normalized_prompt
    assert "full-file cadence/duration/gap quality was not verified" in normalized_prompt
    assert "Missing-value claims must cite `missing_values`" in prompt
    assert "`missing_values_scope=profiled_rows`" in prompt
    assert "Do not turn `qChannel` min/max/mean into" in prompt
    assert "Numeric uncertainty means alone are descriptive statistics" in prompt


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_analysis_keeps_event_context_optional(
    blueprint_id: str,
) -> None:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "clio-agent-marketplace"
        / blueprint_id
        / "experts"
        / "analysis.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    normalized_prompt = " ".join(prompt.split())

    assert "station_network_to_event_context" not in prompt
    assert "next_expert: seismic_event_catalog" not in prompt
    assert "Optional capability:" in prompt
    assert "Request `seismic_event_catalog` only when the user explicitly asks" in prompt
    assert "does not by itself require this child" in normalized_prompt
    assert "do not report event-catalog limitations as a mandatory result" in normalized_prompt


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_event_catalog_prompt_returns_typed_blocker_not_no_events(
    blueprint_id: str,
) -> None:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "clio-agent-marketplace"
        / blueprint_id
        / "experts"
        / "seismic_event_catalog.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    normalized_prompt = " ".join(prompt.split())

    assert "return only an explicit capability gap" in normalized_prompt
    assert "EVENT_CATALOG_BLOCKER: no live event catalog tool available in this pack" in prompt
    assert (
        "Absence of a live event-catalog tool is not evidence that no events occurred"
        in normalized_prompt
    )
    assert "`event_catalog_capability.status=partial`" in prompt
    assert '"event_context"' in prompt
    assert '"status": "blocked"' in prompt
    assert '"verified_event_count": null' in prompt
    assert '"no_live_event_catalog_tool"' in prompt


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_geospatial_prompt_does_not_invent_named_source_provenance(
    blueprint_id: str,
) -> None:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "clio-agent-marketplace"
        / blueprint_id
        / "experts"
        / "geospatial.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    normalized_prompt = " ".join(prompt.split())

    # The geospatial expert may only set a GROUNDED provenance — verbatim user
    # coordinates or a real geocoder lookup — and must never invent coordinates or
    # a source from memory. (The blueprint was generalized: the old
    # `model_geographic_prior` provenance + the named-catalog citation ban were
    # de-hardcoded into these two grounded cases plus the anti-invention rule.)
    assert 'provenance="user-provided"' in prompt
    assert 'provenance="osm_nominatim"' in prompt
    assert "do not fall back to guessing a center from memory" in normalized_prompt
    assert "do NOT invent coordinates" in normalized_prompt


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_resolver_prompt_uses_typed_station_resource_frontier(
    blueprint_id: str,
) -> None:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "external"
        / "clio-agent-marketplace"
        / blueprint_id
        / "experts"
        / "ndp_resource_resolver.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")
    normalized_prompt = " ".join(prompt.split())

    # Per-station search keys on dataset_title with the station id (the prompt
    # explicitly warns AGAINST resource_name for the station id — it 502s).
    assert "in `dataset_title` (NOT `resource_name`, which 502s)" in normalized_prompt
    # The resolver works the ranked station list; it must not widen past it into
    # free-text / city-name searches.
    assert "never widen beyond the ranked list" in normalized_prompt
    # An out-of-region / no-candidate region is not coverage and must not be
    # searched or staged.
    assert "Do NOT search or stage anything, do NOT" in normalized_prompt
    assert "the region has no EarthScope GNSS coverage" in normalized_prompt


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_data_prompt_requires_staged_metadata_before_station_filter(
    blueprint_id: str,
) -> None:
    root = Path(__file__).resolve().parents[2] / "external" / "clio-agent-marketplace"
    data_prompt = (root / blueprint_id / "experts" / "data.md").read_text(encoding="utf-8")
    discovery_prompt = (root / blueprint_id / "experts" / "ndp_dataset_discovery.md").read_text(
        encoding="utf-8"
    )
    station_prompt = (root / blueprint_id / "experts" / "earthscope_station_catalog.md").read_text(
        encoding="utf-8"
    )
    normalized_data = " ".join(data_prompt.split())
    normalized_discovery = " ".join(discovery_prompt.split())
    normalized_station = " ".join(station_prompt.split())

    # Discovery must stage the metadata catalog; the parent routes the work back
    # to discovery when only a guessed filename (not a staged path) is present.
    assert "acquisition.metadata_path" in data_prompt
    assert (
        "a guessed filename such as `earthscope_stations.csv` is not a staged path"
        in normalized_data
    )
    # Discovery must stage + clean the metadata catalog before ranking can run:
    # the cleaned workspace CSV is the station ranker's required input, and the run
    # is explicitly incomplete until that staging pipeline finishes.
    assert "The downstream ranker needs the CLEANED workspace file" in normalized_discovery
    assert (
        "your run is INCOMPLETE until the `pandas_filter_data` clean has run"
        in normalized_discovery
    )
    # The station ranker filters the staged metadata path, never a guessed name
    # (tool renamed: ndp_filter_earthscope_station_catalog -> geo_filter_points_by_radius).
    assert "never filter the raw catalog or a guessed filename" in normalized_station
    assert (
        "the cleaned catalog at `acquisition.metadata_path` with `geo_filter_points_by_radius`"
        in normalized_station
    )


@pytest.mark.parametrize(
    "blueprint_id",
    [
        "earthscope-gnss-region",
    ],
)
def test_earthscope_final_prompts_guard_scan_limited_profile_scope(
    blueprint_id: str,
) -> None:
    root = Path(__file__).resolve().parents[2] / "external" / "clio-agent-marketplace"
    # #948 S4: the react main now writes the final answer itself (no synthesis child),
    # so the answer-quality scan-limited guardrails moved into main.md.
    main_prompt = (root / blueprint_id / "experts" / "main.md").read_text(encoding="utf-8")
    analysis_prompt = (root / blueprint_id / "experts" / "gnss_timeseries_analysis.md").read_text(
        encoding="utf-8"
    )
    visualization_prompt = (root / blueprint_id / "experts" / "visualization.md").read_text(
        encoding="utf-8"
    )
    combined = " ".join("\n".join([main_prompt, analysis_prompt, visualization_prompt]).split())

    assert "rows_profiled`/`numeric_summary_rows" in combined
    assert "appears in `numeric_summary`" in combined
    assert "belongs only to `numeric_summary_rows`" in combined
    assert "say only that the run produced a scan-limited profile" in combined
    assert '"30-s cadence"' in combined
    assert '"no missing values"' in combined
    assert '"low noise"' in combined
    assert "A successful plot proves only that" in combined
    assert "full-file cadence/duration/gap quality was not verified" in combined
    assert "missing_values_scope=profiled_rows" in combined
    assert "Do not interpret `qChannel` numeric values as decoded quality" in combined
    assert "Treat `qChannel` numeric summaries as opaque flag values" in combined
    assert "Uncertainty means are descriptive statistics" in combined
    assert "provenance=model_geographic_prior" in combined
    assert "do not cite USGS, UNAVCO" in combined
    assert "Named source provenance is allowed only when a tool result" in combined


def test_prediction_structured_metadata_omits_empty_values() -> None:
    result = SimpleNamespace(
        workflow_state={"acquisition": {"status": "staged"}},
        evidence="evidence rows",
        errors=None,
        delegation='{"next":"root"}',
    )

    assert _prediction_structured_metadata(result) == {
        "workflow_state": {"acquisition": {"status": "staged"}},
        "evidence": "evidence rows",
        "delegation": '{"next":"root"}',
    }


def test_prediction_workflow_state_read_structurally() -> None:
    # The typed workflow_state output field is read STRUCTURALLY (not serialized
    # into the answer text + re-parsed). _prediction_workflow_state returns the
    # typed field as a plain Mapping for the structured carrier.
    state = _prediction_workflow_state(
        SimpleNamespace(
            workflow_state={
                "acquisition": {
                    "status": "metadata_only",
                    "analysis_ready": False,
                }
            }
        ),
        schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA,
    )

    assert state["acquisition"]["status"] == "metadata_only"
    assert state["acquisition"]["analysis_ready"] is False


def test_prediction_workflow_state_accepts_json_string_and_wrapped_mapping() -> None:
    # A typed field may arrive as a JSON string or already wrapped in
    # {"workflow_state": ...}; both normalize to the inner section mapping.
    from_string = _prediction_workflow_state(
        SimpleNamespace(workflow_state='{"workflow_state": {"profile": {"status": "ready"}}}'),
        schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA,
    )
    assert from_string["profile"]["status"] == "ready"

    from_wrapped = _prediction_workflow_state(
        SimpleNamespace(workflow_state={"workflow_state": {"artifact": {"status": "ready"}}}),
        schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA,
    )
    assert from_wrapped["artifact"]["status"] == "ready"

    assert (
        _prediction_workflow_state(
            SimpleNamespace(workflow_state=""), schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA
        )
        == {}
    )
    assert (
        _prediction_workflow_state(
            SimpleNamespace(workflow_state=None), schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA
        )
        == {}
    )


def test_native_domain_expert_modules_are_not_runtime_importable(tmp_path: Path) -> None:
    retired_modules = [
        "clio_agent.experts.data_expert",
        "clio_agent.experts.analysis_expert",
        "clio_agent.experts.visualization_expert",
        "clio_agent.experts.ndp_expert",
        "clio_agent.experts.sac_format_expert",
    ]

    for module_name in retired_modules:
        assert importlib.util.find_spec(module_name) is None

    agent = ClioAgent(data_dir=str(tmp_path / "clio"))
    try:
        for attr in (
            "data_expert",
            "analysis_expert",
            "visualization_expert",
            "ndp_catalog_expert",
            "sac_format_expert",
        ):
            assert not hasattr(agent, attr)
        assert not {"data", "analysis", "visualization", "ndp_catalog", "sac_format"} & set(
            agent.registry.list_agents()
        )
    finally:
        agent.shutdown()


def test_validate_agent_blueprint_markdown_root(tmp_path: Path) -> None:
    root = tmp_path / "genomics"
    _write_blueprint(root)

    body = validate_agent_blueprint_path(root)

    assert body["enabled"] is True
    assert body["agent_blueprint"]["id"] == "genomics"
    rows = {row["id"]: row for row in body["agents"]}
    assert rows["root"]["tier"] == 1
    assert rows["variant"]["parent_id"] == "root"
    assert rows["variant"]["tools"] == ["memory_search_sessions"]


def test_agent_blueprint_activation_replaces_default_agent_graph(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    blueprint = workspace / ".clio" / "agent-blueprints" / "genomics"
    _write_blueprint(blueprint)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        sid = client.post(
            "/v1/sessions",
            json={"title": "genomics", "workspace_id": wid},
        ).json()["id"]
        activated = client.post(
            f"/v1/sessions/{sid}/agent-blueprint",
            json={"blueprint_id": "genomics"},
        )
        assert activated.status_code == 200, activated.text
        agents = {
            row["id"]: row
            for row in client.get("/v1/agents", params={"session_id": sid}).json()["agents"]
        }

    assert set(agents) == {"root", "variant"}
    assert "data" not in agents
    assert agents["variant"]["metadata"]["agent_blueprint_id"] == "genomics"


def test_agent_blueprint_root_runtime_context_lists_declared_children(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    blueprint = workspace / ".clio" / "agent-blueprints" / "genomics"
    _write_blueprint(blueprint)
    blueprint.joinpath("experts", "root.md").write_text(
        """---
id: root
title: Genomics Root
tier: 1
prompt_id: clio.main.planner
---
Coordinate genomics work.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        sid = client.post(
            "/v1/sessions",
            json={"title": "genomics", "workspace_id": wid},
        ).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid}/agent-blueprint",
                json={"blueprint_id": "genomics"},
            ).status_code
            == 200
        )
        root = next(
            AgentDef(**row)
            for row in client.get("/v1/agents", params={"session_id": sid}).json()["agents"]
            if row["id"] == "root"
        )

    context = _runtime_dynamic_agent_children_context(app, root, session_id=sid)

    assert "Your child experts (delegate to these — you may route to no one else):" in context
    assert "- `variant`: Variant Expert" in context
    assert "memory_search_sessions" in context
    assert "`spawn_agent_task(agent, task)`" in context
    assert "`wait_agent_tasks(" in context
    # Async-first posture lock (async-first-semantics slice): the routing briefing
    # MUST teach the fire-and-forget async spawn posture, not the old serial
    # "spawn one child, wait, decide the next hop" loop. These load-bearing phrases
    # are the taught surface under live test — reverting the paragraph reddens this.
    assert "IMMEDIATELY" in context  # spawn_agent_task returns immediately (non-blocking)
    assert "independent child right away" in context  # spawn all independent children first
    assert "SHORT budget" in context  # bounded wait, decide on a partial
    assert "NEXT turn" in context  # observe-later: results inject into the next turn
    assert "check_agent_tasks" in context  # non-blocking collection while working
    # The old serial teaching must be gone (it made sync spawn→wait the default).
    assert "Spawn one child, wait for its evidence" not in context
    # The evidence-grounding guarantee must survive the rewrite intact.
    assert "must be backed by a child's returned" in context
    assert "no evidence yet" in context
    # The child listing moved out of the static system_prompt into the runtime
    # children context (asserted above); the system_prompt must still have no
    # unresolved template markers.
    assert "{{" not in root.system_prompt


def test_session_agent_overlay_is_session_local(tmp_path: Path) -> None:
    blueprint = tmp_path / "genomics"
    _write_blueprint(blueprint)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        sid_a = client.post("/v1/sessions", json={"title": "A"}).json()["id"]
        sid_b = client.post("/v1/sessions", json={"title": "B"}).json()["id"]
        for sid in (sid_a, sid_b):
            assert (
                client.post(
                    f"/v1/sessions/{sid}/agent-blueprint",
                    json={"path": str(blueprint)},
                ).status_code
                == 200
            )
        saved = client.put(
            f"/v1/sessions/{sid_a}/agent-overlay",
            json={
                "agents": {
                    "variant": {
                        "title": "Session A Variant Expert",
                        "default_model": "gpt-5-mini",
                        "api_base": "https://alt.example.com/v1",
                        "credential_ref": "openai:acctB",
                        "transport": "exec",
                    }
                }
            },
        )
        assert saved.status_code == 200, saved.text
        agent_a = client.get("/v1/agents/variant", params={"session_id": sid_a}).json()
        agent_b = client.get("/v1/agents/variant", params={"session_id": sid_b}).json()

    assert agent_a["title"] == "Session A Variant Expert"
    assert agent_a["default_model"] == "gpt-5-mini"
    # Per-expert provider identity (#818) is patchable through the session overlay.
    assert agent_a["api_base"] == "https://alt.example.com/v1"
    assert agent_a["credential_ref"] == "openai:acctB"
    assert agent_a["transport"] == "exec"
    assert agent_a["metadata"]["agent_blueprint_overlay"]["status"] == "applied"
    assert agent_b["title"] == "Variant Expert"
    assert agent_b["default_model"] == ""
    # A sibling session without the overlay keeps the empty defaults (session-local).
    assert agent_b["api_base"] == ""
    assert agent_b["credential_ref"] == ""
    assert agent_b["transport"] == ""


def test_session_agent_overlay_rejects_invalid_contracts(tmp_path: Path) -> None:
    blueprint = tmp_path / "genomics"
    _write_blueprint(blueprint)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "A"}).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid}/agent-blueprint",
                json={"path": str(blueprint)},
            ).status_code
            == 200
        )

        broken_parent = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={"agents": {"variant": {"parent_id": "missing-parent"}}},
        )
        bad_tool = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={"agents": {"variant": {"tools": ["definitely_missing_tool"]}}},
        )
        bad_provider = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={"agents": {"variant": {"default_provider": "definitely_missing_provider"}}},
        )
        bad_prompt = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={"agents": {"variant": {"prompt_id": "definitely.missing.prompt"}}},
        )
        unknown_agent = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={"agents": {"missing-agent": {"title": "Nope"}}},
        )
        overlay_state = client.get(f"/v1/sessions/{sid}/agent-overlay").json()

    assert broken_parent.status_code == 422
    assert "parent_id not found" in broken_parent.text
    assert bad_tool.status_code == 422
    assert "unknown tool" in bad_tool.text
    assert bad_provider.status_code == 422
    assert "provider not found" in bad_provider.text
    assert bad_prompt.status_code == 422
    assert "prompt not found" in bad_prompt.text
    assert unknown_agent.status_code == 422
    assert "unknown expert" in unknown_agent.text
    assert overlay_state["agent_overlay"] == {}
    assert overlay_state["validation"]["enabled"] is True


def test_agent_blueprint_expert_markdown_round_trips_module_kind(tmp_path: Path) -> None:
    # #948 S4 failing-first regression: a react parent exported WITHOUT its
    # ``module.kind`` re-loads as the ``predict`` default and fails hierarchy
    # validation (children unreachable). Deleting the module block in
    # routes/agents.py::_agent_blueprint_expert_markdown MUST turn this red.
    from clio_agent.gact.expert_packs import parse_expert_file
    from clio_agent.gact.routes.agents import _agent_blueprint_expert_markdown

    row = AgentDef(
        id="root",
        source="expert_pack",
        title="Root",
        tier=1,
        module={"kind": "react"},
        system_prompt="Orchestrate the declared children.",
    )

    md = _agent_blueprint_expert_markdown(row)
    # Direct sabotage check: the serializer emits the module block.
    assert 'module:\n  kind: "react"' in md

    # Round-trip: the exported markdown re-parses to a react module. The loader
    # defaults an ABSENT module to ``predict`` (parse_expert_file -> _module_from_meta),
    # so a dropped serializer block would flip ``kind`` to predict and fail here.
    exported = tmp_path / "root.md"
    exported.write_text(md, encoding="utf-8")
    reparsed = parse_expert_file(exported, scope="workspace")
    assert reparsed.module["kind"] == "react"


def test_session_agent_overlay_can_export_workspace_blueprint(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = tmp_path / "source" / "genomics"
    _write_blueprint(source)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        sid = client.post(
            "/v1/sessions",
            json={"title": "A", "workspace_id": wid},
        ).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid}/agent-blueprint",
                json={"path": str(source)},
            ).status_code
            == 200
        )
        saved = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={
                "agents": {
                    "variant": {
                        "title": "Session A Variant Expert",
                        "system_prompt": "Review this workspace's variant evidence.",
                    }
                }
            },
        )
        assert saved.status_code == 200, saved.text

        exported = client.post(
            f"/v1/sessions/{sid}/agent-overlay/export",
            json={
                "blueprint_id": "genomics-session-a",
                "title": "Genomics Session A",
                "workspace_id": wid,
            },
        )
        listed = client.get("/v1/agent-blueprints", params={"workspace_id": wid}).json()

    assert exported.status_code == 201, exported.text
    exported_root = workspace / ".clio" / "agent-blueprints" / "genomics-session-a"
    assert exported_root.joinpath("AGENT.md").exists()
    assert "Session A Variant Expert" in exported_root.joinpath("experts", "variant.md").read_text()
    assert "Variant Expert" in source.joinpath("experts", "variant.md").read_text()
    assert {row["id"] for row in exported.json()["agents"]} == {"root", "variant"}
    assert "genomics-session-a" in {row["id"] for row in listed["agent_blueprints"]}


def test_session_agent_overlay_prompt_provenance_reaches_prompts_and_turn_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    blueprint = tmp_path / "remote-data"
    _write_data_root_blueprint(blueprint)
    calls: list[dict[str, str]] = []

    async def no_stream(*args, **kwargs):
        return None

    def fake_blueprint_runner(base_agent, agent_def, question, session_id, cancel_requested=None):
        del base_agent, cancel_requested
        calls.append(
            {
                "agent_id": agent_def.id,
                "system_prompt": agent_def.system_prompt,
                "model": agent_def.default_model,
                "question": question,
                "session_id": session_id,
            }
        )
        return SimpleNamespace(
            answer="overlay provenance ok",
            selected_expert=agent_def.id,
            routing_rationale="session overlay",
            route_source="agent_blueprint",
            error_info=None,
        )

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", no_stream)
    monkeypatch.setattr("clio_agent.gact.app._run_blueprint_dspy_agent", fake_blueprint_runner)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "overlay runtime"}).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid}/agent-blueprint",
                json={"path": str(blueprint)},
            ).status_code
            == 200
        )
        saved = client.put(
            f"/v1/sessions/{sid}/agent-overlay",
            json={
                "agents": {
                    "data": {
                        "system_prompt": "SESSION OVERLAY PROMPT.",
                        "default_model": "gpt-5-mini",
                    }
                }
            },
        )
        assert saved.status_code == 200, saved.text
        prompts = client.get("/v1/prompts", params={"session_id": sid}).json()
        assistant = complete_turn(client, sid, "prove overlay provenance")

    overlay_sources = prompts["agent_overlay"]["agents"]
    assert overlay_sources == [
        {
            "agent_id": "data",
            "fields": ["default_model", "system_prompt"],
            "has_system_prompt": True,
            "prompt_id": "",
            "prompt_profile": "",
            "default_provider": "",
            "default_model": "gpt-5-mini",
            "source": "session_agent_overlay",
            "session_id": sid,
        }
    ]
    assert calls == [
        {
            "agent_id": "data",
            "system_prompt": "SESSION OVERLAY PROMPT.",
            "model": "gpt-5-mini",
            "question": "prove overlay provenance",
            "session_id": sid,
        }
    ]
    runtime = assistant["metadata"]["agent_runtime"]
    assert runtime["agent_blueprint"]["id"] == "remote-data"
    assert runtime["agent_overlay"]["status"] == "applied"
    assert runtime["agent_overlay"]["fields"] == ["default_model", "system_prompt"]
    assert runtime["prompt"]["source"] == "session_agent_overlay"


def test_agent_blueprint_install_from_local_marketplace(tmp_path: Path) -> None:
    marketplace = tmp_path / "marketplace"
    _write_blueprint(marketplace / "genomics")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        installed = client.post(
            "/v1/agent-blueprints/install",
            json={"source": str(marketplace), "scope": "workspace", "workspace_id": wid},
        )
        assert installed.status_code == 201, installed.text
        listed = client.get("/v1/agent-blueprints", params={"workspace_id": wid}).json()

    ids = {row["id"] for row in listed["agent_blueprints"]}
    assert "genomics" in ids
    assert (workspace / ".clio" / "agent-blueprints" / "genomics" / ".clio-install.md").exists()


def test_agent_blueprint_marketplace_sources_persist_and_install_by_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # Isolate the per-user config dir (where the source ledger persists) with
    # the cross-OS ``CLIO_USER_DIR`` override; ``XDG_CONFIG_HOME`` is honored
    # only on Linux and would let this test read the developer's real store on
    # Windows/macOS (104 accumulated sources instead of the one written here).
    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path / "user-config"))
    marketplace = tmp_path / "marketplace"
    _write_blueprint(marketplace / "genomics")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        created = client.post(
            "/v1/agent-blueprints/sources",
            json={
                "source": str(marketplace),
                "name": "Local marketplace",
                "pinned_commit": "",
            },
        )
        assert created.status_code == 201, created.text
        source = created.json()["source"]
        source_id = source["id"]
        assert source["status"] == "ready"
        assert source["source_kind"] == "path"
        assert source["pinned_commit"] == ""
        assert [row["id"] for row in source["available_blueprints"]] == ["genomics"]

        listed = client.get("/v1/agent-blueprints/sources").json()
        assert [row["id"] for row in listed["sources"]] == [source_id]

        refreshed = client.post(f"/v1/agent-blueprints/sources/{source_id}/refresh")
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["source"]["available_blueprints"][0]["id"] == "genomics"

        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        installed = client.post(
            "/v1/agent-blueprints/install",
            json={"source_id": source_id, "scope": "workspace", "workspace_id": wid},
        )
        assert installed.status_code == 201, installed.text

        deleted = client.delete(f"/v1/agent-blueprints/sources/{source_id}")
        assert deleted.status_code == 200, deleted.text
        assert client.get("/v1/agent-blueprints/sources").json()["sources"] == []

    assert (workspace / ".clio" / "agent-blueprints" / "genomics" / ".clio-install.md").exists()


def test_marketplace_install_supports_distinct_session_blueprints(tmp_path: Path) -> None:
    marketplace = tmp_path / "marketplace"
    _write_blueprint(marketplace / "genomics-review", blueprint_id="genomics-review")
    _write_data_root_blueprint(
        marketplace / "materials-crystal-review",
        blueprint_id="materials-crystal-review",
    )
    workspace = tmp_path / "workspace"
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        install = client.post(
            "/v1/agent-blueprints/install",
            json={"source": str(marketplace), "scope": "workspace", "workspace_id": wid},
        )
        assert install.status_code == 201, install.text
        installed = {row["id"] for row in install.json()["installed"]}
        assert installed == {"genomics-review", "materials-crystal-review"}

        sid_genomics = client.post(
            "/v1/sessions",
            json={"title": "genomics", "workspace_id": wid},
        ).json()["id"]
        sid_materials = client.post(
            "/v1/sessions",
            json={"title": "materials", "workspace_id": wid},
        ).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid_genomics}/agent-blueprint",
                json={"blueprint_id": "genomics-review"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/v1/sessions/{sid_materials}/agent-blueprint",
                json={"blueprint_id": "materials-crystal-review"},
            ).status_code
            == 200
        )

        genomics = client.get(f"/v1/sessions/{sid_genomics}/agent-blueprint").json()
        materials = client.get(f"/v1/sessions/{sid_materials}/agent-blueprint").json()
        genomics_agents = client.get(
            "/v1/agents",
            params={"session_id": sid_genomics},
        ).json()["agents"]
        materials_agents = client.get(
            "/v1/agents",
            params={"session_id": sid_materials},
        ).json()["agents"]

    assert genomics["active_agent_blueprint_id"] == "genomics-review"
    assert materials["active_agent_blueprint_id"] == "materials-crystal-review"
    assert genomics["activation"]["active_agent_blueprint_source"] == str(marketplace)
    assert genomics["activation"]["active_agent_blueprint_source_kind"] == "path"
    assert genomics["activation"]["active_agent_blueprint_checksum"]
    assert genomics["activation"]["active_agent_blueprint_installed_at"]
    assert materials["activation"]["active_agent_blueprint_source"] == str(marketplace)
    assert materials["activation"]["active_agent_blueprint_checksum"]
    assert {row["id"] for row in genomics_agents} == {"root", "variant"}
    assert {row["id"] for row in materials_agents} == {"data"}


def test_agent_blueprint_install_from_git_marketplace_records_pinned_metadata(
    tmp_path: Path,
) -> None:
    marketplace = tmp_path / "marketplace"
    _write_blueprint(marketplace / "genomics")
    subprocess.run(["git", "init"], cwd=marketplace, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "add", "."], cwd=marketplace, check=True, stdout=subprocess.PIPE)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=CLIO Test",
            "-c",
            "user.email=clio@example.invalid",
            "commit",
            "-m",
            "Add genomics blueprint",
        ],
        cwd=marketplace,
        check=True,
        stdout=subprocess.PIPE,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(marketplace), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        installed = client.post(
            "/v1/agent-blueprints/install",
            json={
                "source": marketplace.as_uri(),
                "scope": "workspace",
                "workspace_id": wid,
                "blueprint_id": "genomics",
            },
        )

    assert installed.status_code == 201, installed.text
    metadata = installed.json()["installed"][0]["install"]
    assert metadata["source_kind"] == "git"
    assert metadata["source"] == marketplace.as_uri()
    assert metadata["commit"] == commit
    assert metadata["checksum"]
    assert metadata["installed_at"]


def test_agent_blueprint_update_and_delete_installed_blueprint(tmp_path: Path) -> None:
    marketplace = tmp_path / "marketplace"
    _write_blueprint(marketplace / "genomics")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        assert (
            client.post(
                "/v1/agent-blueprints/install",
                json={"source": str(marketplace), "scope": "workspace", "workspace_id": wid},
            ).status_code
            == 201
        )
        marketplace.joinpath("genomics", "experts", "variant.md").write_text(
            """---
id: variant
title: Updated Variant Expert
parent_id: root
tier: 2
---
Updated behavior.
""",
            encoding="utf-8",
        )
        updated = client.post(
            "/v1/agent-blueprints/genomics/update",
            json={"scope": "workspace", "workspace_id": wid},
        )
        assert updated.status_code == 200, updated.text
        assert (
            "Updated Variant Expert"
            in (
                workspace / ".clio" / "agent-blueprints" / "genomics" / "experts" / "variant.md"
            ).read_text()
        )
        deleted = client.delete(
            "/v1/agent-blueprints/genomics",
            params={"scope": "workspace", "workspace_id": wid},
        )
        assert deleted.status_code == 200, deleted.text

    assert not (workspace / ".clio" / "agent-blueprints" / "genomics").exists()


def test_expert_pack_lifecycle_aliases_blueprint_engine(tmp_path: Path) -> None:
    # iowarp/clio-agent#663: /v1/expert-packs/* install/update/delete are thin
    # aliases of the one agent-blueprint lifecycle engine; installed rows carry
    # a kind discriminator. A blueprint (explicit root_expert) -> kind="blueprint".
    marketplace = tmp_path / "marketplace"
    _write_blueprint(marketplace / "genomics")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "W",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        resp = client.post(
            "/v1/expert-packs/install",
            json={"source": str(marketplace), "scope": "workspace", "workspace_id": wid},
        )
        assert resp.status_code == 201, resp.text
        installed = resp.json()["installed"]
        assert installed and installed[0]["kind"] == "blueprint", installed

        upd = client.post(
            "/v1/expert-packs/genomics/update",
            json={"scope": "workspace", "workspace_id": wid},
        )
        assert upd.status_code == 200, upd.text

        deleted = client.delete(
            "/v1/expert-packs/genomics",
            params={"scope": "workspace", "workspace_id": wid},
        )
        assert deleted.status_code == 200, deleted.text
    assert not (workspace / ".clio" / "agent-blueprints" / "genomics").exists()


def test_expert_pack_kind_is_pack_without_root_orchestrator(tmp_path: Path) -> None:
    # A loose pack: AGENT.md with NO explicit root_expert + a single root
    # expert -> installs through the shared engine, kind == "pack".
    pack = tmp_path / "marketplace" / "toolkit"
    (pack / "experts").mkdir(parents=True)
    pack.joinpath("AGENT.md").write_text(
        """---
id: toolkit
version: 0.1.0
title: Toolkit Pack
---
A loose pack of experts.
""",
        encoding="utf-8",
    )
    pack.joinpath("experts", "helper.md").write_text(
        """---
id: helper
title: Helper Expert
tier: 1
---
A helper.
""",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "W",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        resp = client.post(
            "/v1/expert-packs/install",
            json={"source": str(pack.parent), "scope": "workspace", "workspace_id": wid},
        )
        assert resp.status_code == 201, resp.text
        kinds = {r["id"]: r["kind"] for r in resp.json()["installed"]}
        assert kinds.get("toolkit") == "pack", kinds
        listed = client.get("/v1/expert-packs", params={"workspace_id": wid})
        assert listed.status_code == 200, listed.text
        listed_rows = {row["id"]: row for row in listed.json()["expert_packs"]}
        assert listed_rows["toolkit"]["kind"] == "pack"

        detail = client.get("/v1/expert-packs/toolkit", params={"workspace_id": wid})
        assert detail.status_code == 200, detail.text
        assert detail.json()["expert_pack"]["kind"] == "pack"
        assert [row["id"] for row in detail.json()["agents"]] == ["helper"]


def test_active_agent_blueprint_drives_turn_runtime_and_overrides_builtin_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    blueprint = workspace / ".clio" / "agent-blueprints" / "remote-data"
    _write_data_root_blueprint(blueprint)
    calls: list[dict[str, str]] = []

    async def no_stream(*args, **kwargs):
        return None

    def fake_blueprint_runner(base_agent, agent_def, question, session_id, cancel_requested=None):
        del base_agent, cancel_requested
        calls.append(
            {
                "agent_id": agent_def.id,
                "title": agent_def.title,
                "system_prompt": agent_def.system_prompt,
                "question": question,
                "session_id": session_id,
            }
        )
        return SimpleNamespace(
            answer=f"runtime from {agent_def.id}",
            selected_expert=agent_def.id,
            routing_rationale="session blueprint",
            route_source="agent_blueprint",
            error_info=None,
        )

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", no_stream)
    monkeypatch.setattr("clio_agent.gact.app._run_blueprint_dspy_agent", fake_blueprint_runner)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        sid_blueprint = client.post(
            "/v1/sessions",
            json={"title": "blueprint", "workspace_id": wid},
        ).json()["id"]
        sid_builtin = client.post(
            "/v1/sessions",
            json={"title": "builtin", "workspace_id": wid},
        ).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid_blueprint}/agent-blueprint",
                json={"blueprint_id": "remote-data"},
            ).status_code
            == 200
        )
        agents_blueprint = client.get(
            "/v1/agents",
            params={"session_id": sid_blueprint},
        ).json()["agents"]
        agents_builtin = client.get(
            "/v1/agents",
            params={"session_id": sid_builtin},
        ).json()["agents"]
        assistant = complete_turn(client, sid_blueprint, "prove runtime")

    assert [row["id"] for row in agents_blueprint] == ["data"]
    # DELIBERATE FLIP: this used to assert the bare sibling session ("builtin",
    # no activation) surfaced "analysis" -- which only ever came from
    # catalog._builtin_agents() silently loading the conftest's installed
    # default-registry snapshot (autouse allow_pytest_tmp_path fixture). Now that
    # implicit load is deleted, the bare session honestly shows only the
    # code-shipped builtin main, proving session-scoped activation isolation:
    # sid_blueprint's activation of "remote-data" never leaks into sid_builtin.
    assert [row["id"] for row in agents_builtin] == ["main"]
    assert calls == [
        {
            "agent_id": "data",
            "title": "Remote Data Orchestrator",
            "system_prompt": "REMOTE BLUEPRINT ORCHESTRATOR MARKER.",
            "question": "prove runtime",
            "session_id": sid_blueprint,
        }
    ]
    assert assistant["metadata"]["agent_runtime"]["agent_id"] == "data"
    assert assistant["metadata"]["agent_runtime"]["source"] == "expert_pack"
    assert assistant["metadata"]["agent_runtime"]["pack"]["id"] == "remote-data"


def test_agent_blueprint_mcp_descriptor_installs_disabled(tmp_path: Path) -> None:
    root = tmp_path / "marketplace" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    (root / "tools").mkdir()
    root.joinpath("tools", "earthscope.md").write_text(
        """---
id: earthscope
name: EarthScope MCP
transport: stdio
command: earthscope-mcp
args:
  - serve
---
EarthScope descriptor.
""",
        encoding="utf-8",
    )

    body = validate_agent_blueprint_path(root)

    descriptor = body["mcp_descriptors"][0]
    assert descriptor["id"] == "earthscope"
    assert descriptor["enabled"] is False
    assert descriptor["status"] == "disabled"
    assert descriptor["transport"] == "stdio"


def test_agent_blueprint_mcp_tool_references_require_enablement(tmp_path: Path) -> None:
    root = tmp_path / "marketplace" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    root.joinpath("experts", "variant.md").write_text(
        """---
id: variant
title: Variant Expert
parent_id: root
tier: 2
tools:
  - earthscope_query
---
Use the external EarthScope catalog.
""",
        encoding="utf-8",
    )
    (root / "tools").mkdir()
    root.joinpath("tools", "earthscope.md").write_text(
        """---
id: earthscope
name: EarthScope MCP
transport: stdio
command: earthscope-mcp
args:
  - serve
tools:
  - earthscope_query
---
EarthScope descriptor.
""",
        encoding="utf-8",
    )

    body = validate_agent_blueprint_path(root)
    rows = {row["id"]: row for row in body["agents"]}

    assert body["enabled"] is False
    assert "MCP tool requires explicit enablement" in "\n".join(body["validation_errors"])
    assert rows["variant"]["enabled"] is False
    assert rows["variant"]["metadata"]["tool_diagnostics"][0]["tool"] == "earthscope_query"
    assert body["mcp_descriptors"][0]["tools"][0]["status"] == "disabled"


def test_agent_blueprint_validation_reports_unknown_tools(tmp_path: Path) -> None:
    root = tmp_path / "marketplace" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    root.joinpath("experts", "variant.md").write_text(
        """---
id: variant
title: Variant Expert
parent_id: root
tier: 2
tools:
  - missing_external_tool
---
Use an undeclared external tool.
""",
        encoding="utf-8",
    )

    body = validate_agent_blueprint_path(root)

    assert body["enabled"] is False
    assert "unknown tool reference: missing_external_tool" in "\n".join(body["validation_errors"])


def test_agent_blueprint_mcp_descriptor_requires_explicit_enablement(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    root = workspace / ".clio" / "agent-blueprints" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    (root / "tools").mkdir()
    root.joinpath("tools", "earthscope.md").write_text(
        """---
id: earthscope
name: EarthScope MCP
transport: stdio
command: earthscope-mcp
args:
  - serve
---
EarthScope descriptor.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        rows_before = client.get("/v1/mcp/servers", params={"workspace_id": wid}).json()["servers"]
        enabled = client.post(
            "/v1/agent-blueprints/earth/mcp/earthscope/enable",
            json={"workspace_id": wid, "probe": False},
        )
        assert enabled.status_code == 200, enabled.text
        rows_after = client.get("/v1/mcp/servers", params={"workspace_id": wid}).json()["servers"]

    assert any(
        row.get("source") == "agent_blueprint" and row.get("enabled") is False
        for row in rows_before
    )
    assert enabled.json()["status"] == "enabled_pending_probe"
    assert any(row["id"] == "agent_blueprint_mcp_earth_earthscope" for row in rows_after)


def test_enabled_agent_blueprint_mcp_descriptor_exposes_declared_tools(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    root = workspace / ".clio" / "agent-blueprints" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    (root / "tools").mkdir()
    root.joinpath("tools", "earthscope.md").write_text(
        """---
id: earthscope
name: EarthScope MCP
transport: stdio
command: earthscope-mcp
args:
  - serve
tools:
  - earthscope_query
---
EarthScope descriptor.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        enabled = client.post(
            "/v1/agent-blueprints/earth/mcp/earthscope/enable",
            json={"workspace_id": wid, "probe": False},
        )
        tools = client.get("/v1/tools").json()["tools"]
        detail = client.get("/v1/tools/earthscope_query").json()

    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["tools_count"] == 1
    declared = next(row for row in tools if row["id"] == "earthscope_query")
    assert declared["source"] == "agent_blueprint_mcp_descriptor"
    assert declared["status"] == "enabled_pending_probe"
    assert declared["enabled"] is False
    assert detail["descriptor_id"] == "earthscope"


def test_enabled_agent_blueprint_mcp_descriptor_probes_and_calls_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeClient:
        called_tool = ""

        def __init__(self, transport: Any, **_: Any) -> None:
            self.transport = transport

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

        async def list_tools(self) -> list[Any]:
            return [
                SimpleNamespace(
                    name="earthscope_query",
                    description="query EarthScope catalog",
                    inputSchema={"type": "object"},
                    outputSchema={"type": "object"},
                    annotations={"readOnlyHint": True},
                )
            ]

        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            FakeClient.called_tool = name
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=f"{name}:{args['q']}")],
                isError=False,
            )

    import fastmcp
    import fastmcp.client.transports as transports

    monkeypatch.setattr(fastmcp, "Client", FakeClient)
    monkeypatch.setattr(
        transports, "StdioTransport", lambda command, args, env=None: (command, args)
    )

    workspace = tmp_path / "workspace"
    root = workspace / ".clio" / "agent-blueprints" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    (root / "tools").mkdir()
    root.joinpath("tools", "earthscope.md").write_text(
        """---
id: earthscope
name: EarthScope MCP
transport: stdio
command: earthscope-mcp
args:
  - serve
tools:
  - earthscope_query
---
EarthScope descriptor.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        enabled = client.post(
            "/v1/agent-blueprints/earth/mcp/earthscope/enable",
            json={"workspace_id": wid},
        )
        call = client.post(
            "/v1/mcp/servers/agent_blueprint_mcp_earth_earthscope/call",
            json={"tool": "earthscope_query", "args": {"q": "ANMO"}},
        )
        tools = client.get("/v1/tools").json()["tools"]

    assert enabled.status_code == 200, enabled.text
    body = enabled.json()
    assert body["status"] == "ready"
    assert body["tools"][0]["enabled"] is True
    assert body["tools"][0]["input_schema"] == {"type": "object"}
    assert body["tools"][0]["annotations"] == {"readOnlyHint": True}
    assert call.status_code == 200, call.text
    assert FakeClient.called_tool == "earthscope_query"
    assert call.json()["content"] == [{"type": "text", "text": "earthscope_query:ANMO"}]
    declared = next(row for row in tools if row["id"] == "earthscope_query")
    assert declared["enabled"] is True
    assert declared["status"] == "ready"


def test_enabled_agent_blueprint_mcp_tool_reenables_session_expert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self, transport: Any, **_: Any) -> None:
            self.transport = transport

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

        async def list_tools(self) -> list[Any]:
            return [
                SimpleNamespace(
                    name="earthscope_query",
                    description="query EarthScope catalog",
                    inputSchema={"type": "object"},
                    outputSchema={"type": "object"},
                )
            ]

    import fastmcp
    import fastmcp.client.transports as transports

    monkeypatch.setattr(fastmcp, "Client", FakeClient)
    monkeypatch.setattr(
        transports, "StdioTransport", lambda command, args, env=None: (command, args)
    )

    workspace = tmp_path / "workspace"
    root = workspace / ".clio" / "agent-blueprints" / "earth"
    _write_blueprint(root, blueprint_id="earth")
    root.joinpath("experts", "variant.md").write_text(
        """---
id: variant
title: Variant Expert
parent_id: root
tier: 2
tools:
  - earthscope_query
---
Use the external EarthScope catalog.
""",
        encoding="utf-8",
    )
    (root / "tools").mkdir()
    root.joinpath("tools", "earthscope.md").write_text(
        """---
id: earthscope
name: EarthScope MCP
transport: stdio
command: earthscope-mcp
args:
  - serve
tools:
  - earthscope_query
---
EarthScope descriptor.
""",
        encoding="utf-8",
    )

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        sid = client.post(
            "/v1/sessions",
            json={"title": "earth", "workspace_id": wid},
        ).json()["id"]
        assert (
            client.post(
                f"/v1/sessions/{sid}/agent-blueprint",
                json={"blueprint_id": "earth"},
            ).status_code
            == 200
        )
        before = {
            row["id"]: row
            for row in client.get("/v1/agents", params={"session_id": sid}).json()["agents"]
        }
        enabled = client.post(
            "/v1/agent-blueprints/earth/mcp/earthscope/enable",
            json={"workspace_id": wid},
        )
        after = {
            row["id"]: row
            for row in client.get("/v1/agents", params={"session_id": sid}).json()["agents"]
        }

    assert before["variant"]["enabled"] is False
    assert "MCP tool requires explicit enablement" in "\n".join(
        before["variant"]["validation_errors"]
    )
    assert enabled.status_code == 200, enabled.text
    assert after["variant"]["enabled"] is True
    assert after["variant"]["validation_errors"] == []
    assert "tool_diagnostics" not in after["variant"]["metadata"]


def test_dynamic_agent_tools_include_enabled_agent_blueprint_mcp_tool(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    app.state.external_mcp_servers = {
        "agent_blueprint_mcp_earth_earthscope": {
            "id": "agent_blueprint_mcp_earth_earthscope",
            "name": "EarthScope MCP",
            "status": "ready",
            "spec": {
                "transport": "stdio",
                "command": "earthscope-mcp",
                "args": ["serve"],
            },
            "tools": [
                {
                    "id": "earthscope_query",
                    "name": "earthscope_query",
                    "description": "query EarthScope catalog",
                    "status": "ready",
                    "enabled": True,
                    "input_schema": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                    },
                }
            ],
        }
    }
    base_agent = SimpleNamespace(
        tool_executor=SimpleNamespace(to_dspy_tools=lambda: []),
    )
    agent_def = AgentDef(
        id="variant",
        source="expert_pack",
        title="Variant",
        tools=["earthscope_query"],
    )

    with _gact_app_context(app):
        tools = _dynamic_agent_tools(base_agent, agent_def, {})

    assert [tool.name for tool in tools] == ["earthscope_query"]


def test_dynamic_agent_tools_degrades_one_unprojected_tool_instead_of_bricking(
    tmp_path: Path,
) -> None:
    """FAILING-FIRST for #1228 D3 (second half).

    ``relay_artifact_lineage`` / ``relay_status`` were advertised by the live
    relay door but absent from the agent-projected surface -- so an agent ACL
    naming EITHER alongside an otherwise-fine tool made the WHOLE agent
    unexecutable (``_UnsupportedSessionAgent`` / ``custom_agent_tools_unavailable``,
    a hard failure) instead of a typed per-tool absence. This test does not
    depend on the projection fix (the first D3 half): it proves the general
    ACL-degrade contract directly against ``_dynamic_agent_tools`` with a tool
    executor that resolves only ONE of two requested tools.

    Before the fix: this raised. After: the build returns the one tool that
    DID resolve, and the missing one is recorded where it is queryable after
    the fact (this module's structured reason catalog), never silently
    dropped and never fatal to the tools that did resolve.
    """

    app = build_app(sessions_path=tmp_path / "sessions.json")

    class _Tool:
        name = "hdf5_list_datasets"

    class _Executor:
        def to_dspy_tools(self) -> list[Any]:
            return [_Tool()]

    base_agent = SimpleNamespace(tool_executor=_Executor())
    agent_def = AgentDef(
        id="tool_reviewer",
        source="expert_pack",
        title="Tool Reviewer",
        tools=["hdf5_list_datasets", "relay_status"],
    )

    with _gact_app_context(app):
        tools = _dynamic_agent_tools(base_agent, agent_def, {})

    assert [tool.name for tool in tools] == ["hdf5_list_datasets"]
    reasons = toolset_inventory.toolset_inventory_reasons(app, "")
    assert {
        "reason": "custom_agent_tool_unavailable",
        "agent_id": "tool_reviewer",
        "detail": "relay_status",
    } in reasons


def test_dynamic_agent_tools_still_bricks_when_nothing_resolves(tmp_path: Path) -> None:
    """Unchanged regression: an ACL where NOTHING resolves has no reduced
    toolset to degrade to, so it still fails typed -- #1228 D3's degrade only
    applies when at least one requested tool is usable."""

    app = build_app(sessions_path=tmp_path / "sessions.json")

    class _Tool:
        name = "hdf5_list_datasets"

    class _Executor:
        def to_dspy_tools(self) -> list[Any]:
            return [_Tool()]

    base_agent = SimpleNamespace(tool_executor=_Executor())
    agent_def = AgentDef(
        id="tool_reviewer",
        source="expert_pack",
        title="Tool Reviewer",
        tools=["fs_read_file"],
    )

    with _gact_app_context(app), pytest.raises(_UnsupportedSessionAgent) as raised:
        _dynamic_agent_tools(base_agent, agent_def, {})

    assert raised.value.reason == "custom_agent_tools_unavailable"
    assert raised.value.tools == ["fs_read_file"]


def test_active_base_agent_tool_executor_prefers_per_workspace() -> None:
    """A bound workspace routes dynamic-agent tools to the per-workspace executor.

    Blueprint/expert tools are bound to a concrete executor instance, so the
    cwd of the executor's stdio MCP subprocesses is fixed at bind time. When a
    workspace is active, the resolver must hand back the agent's per-workspace
    executor (whose stdio MCPs spawn with cwd=workspace) rather than the shared
    default executor; otherwise staged artifacts land in the server cwd.
    """
    from clio_agent.tools.execution import tool_workspace_context

    default_executor = object()
    workspace_executor = object()
    base_agent = SimpleNamespace(
        tool_executor=default_executor,
        _active_tool_executor=lambda: (
            workspace_executor
            if __import__(
                "clio_agent.tools.execution",
                fromlist=["get_active_tool_workspace_root"],
            ).get_active_tool_workspace_root()
            else default_executor
        ),
    )

    # No workspace bound -> default executor (current behavior).
    assert _active_base_agent_tool_executor(base_agent) is default_executor

    # Workspace bound -> per-workspace executor takes effect.
    with tool_workspace_context("/ws/alpha"):
        assert _active_base_agent_tool_executor(base_agent) is workspace_executor


def test_active_base_agent_tool_executor_falls_back_without_seam() -> None:
    """Agents lacking the per-workspace seam keep using the default executor."""
    default_executor = object()
    base_agent = SimpleNamespace(tool_executor=default_executor)
    assert _active_base_agent_tool_executor(base_agent) is default_executor


# --------------------------------------------------------------------------- #
# #1192: GET /v1/agent-blueprints/{id}/files + .../files/read -- the explorer  #
# surface behind the blueprint window (previously metadata-only routes).      #
# --------------------------------------------------------------------------- #


def _install_genomics_blueprint(tmp_path: Path, client: Any) -> tuple[str, Path]:
    """Install ``genomics`` (AGENT.md + experts/root.md + experts/variant.md)
    into a fresh workspace; returns ``(workspace_id, marketplace_source_root)``.
    """

    marketplace = tmp_path / "marketplace"
    _write_blueprint(marketplace / "genomics")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    wid = client.post(
        "/v1/workspaces",
        json={
            "name": "Workspace",
            "root_path": str(workspace),
            "storage_root": str(workspace / ".clio"),
        },
    ).json()["id"]
    installed = client.post(
        "/v1/agent-blueprints/install",
        json={"source": str(marketplace / "genomics"), "scope": "workspace", "workspace_id": wid},
    )
    assert installed.status_code == 201, installed.text
    return wid, marketplace / "genomics"


def test_agent_blueprint_files_lists_agent_md_and_experts(tmp_path: Path) -> None:
    """Listing is a flat recursive walk: AGENT.md + experts/*.md, relative paths."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid, _ = _install_genomics_blueprint(tmp_path, client)
        listed = client.get("/v1/agent-blueprints/genomics/files", params={"workspace_id": wid})

    assert listed.status_code == 200, listed.text
    rows = {row["path"]: row for row in listed.json()["entries"]}
    assert "AGENT.md" in rows
    assert rows["AGENT.md"]["type"] == "file"
    assert isinstance(rows["AGENT.md"]["size"], int) and rows["AGENT.md"]["size"] > 0
    assert "experts" in rows
    assert rows["experts"]["type"] == "dir"
    assert "experts/root.md" in rows
    assert rows["experts/root.md"]["type"] == "file"
    assert "experts/variant.md" in rows
    assert rows["experts/variant.md"]["type"] == "file"


def test_agent_blueprint_files_read_returns_raw_markdown(tmp_path: Path) -> None:
    """Read serves the raw file content decoded as text/plain (#673/#676 convention)."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid, _ = _install_genomics_blueprint(tmp_path, client)
        read = client.get(
            "/v1/agent-blueprints/genomics/files/read",
            params={"workspace_id": wid, "path": "experts/root.md"},
        )

    assert read.status_code == 200, read.text
    assert read.headers["content-type"].startswith("text/plain")
    assert "Coordinate genomics work." in read.text
    assert "id: root" in read.text


def test_agent_blueprint_files_read_rejects_path_traversal(tmp_path: Path) -> None:
    """A ``..`` escape past the blueprint root is a typed 400, never a 200/500."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid, _ = _install_genomics_blueprint(tmp_path, client)
        escaped = client.get(
            "/v1/agent-blueprints/genomics/files/read",
            params={"workspace_id": wid, "path": "../../../../etc/passwd"},
        )

    assert escaped.status_code == 400, escaped.text
    body = escaped.json()
    assert body["error"]["error"] == "path_outside_blueprint"


def test_agent_blueprint_files_read_missing_file_is_404(tmp_path: Path) -> None:
    """A well-formed relative path that does not exist under the root is a typed 404."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid, _ = _install_genomics_blueprint(tmp_path, client)
        missing = client.get(
            "/v1/agent-blueprints/genomics/files/read",
            params={"workspace_id": wid, "path": "experts/does-not-exist.md"},
        )

    assert missing.status_code == 404, missing.text
    assert missing.json()["error"]["error"] == "not_found"


def test_agent_blueprint_files_unknown_id_is_404(tmp_path: Path) -> None:
    """Listing (and reading) an unknown blueprint id is a typed 404, not an empty list."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid, _ = _install_genomics_blueprint(tmp_path, client)
        listed = client.get(
            "/v1/agent-blueprints/does-not-exist/files", params={"workspace_id": wid}
        )
        read = client.get(
            "/v1/agent-blueprints/does-not-exist/files/read",
            params={"workspace_id": wid, "path": "AGENT.md"},
        )

    assert listed.status_code == 404, listed.text
    assert listed.json()["error"]["error"] == "not_found"
    assert read.status_code == 404, read.text
    assert read.json()["error"]["error"] == "not_found"


def test_agent_blueprint_files_session_scoped_path_activation_resolves(
    tmp_path: Path,
) -> None:
    """#1192 demo case: a blueprint activated by ON-DISK PATH (not an installed
    id -- never discoverable via the catalog) resolves its files ONLY through
    the session-scoped seam (``session_id`` + ``metadata.active_agent_blueprint_path``),
    mirroring how ``earthscope-flat`` is activated in the desktop demo.
    """

    external_root = tmp_path / "external" / "earthscope-flat"
    _write_blueprint(external_root, blueprint_id="earthscope-flat")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace",
                "root_path": str(workspace),
                "storage_root": str(workspace / ".clio"),
            },
        ).json()["id"]
        sid = client.post("/v1/sessions", json={"workspace_id": wid, "title": "demo"}).json()["id"]
        activated = client.post(
            f"/v1/sessions/{sid}/agent-blueprint",
            json={"path": str(external_root / "AGENT.md")},
        )
        assert activated.status_code == 200, activated.text
        assert activated.json()["active_agent_blueprint_id"] == "earthscope-flat"

        # Never installed into any catalog root -- the bare (no session_id)
        # lookup is a typed 404, proving the session-scoped assertion below
        # exercises the path-activation seam and not an accidental catalog hit.
        bare = client.get(
            "/v1/agent-blueprints/earthscope-flat/files", params={"workspace_id": wid}
        )
        assert bare.status_code == 404, bare.text

        listed = client.get(
            "/v1/agent-blueprints/earthscope-flat/files", params={"session_id": sid}
        )
        assert listed.status_code == 200, listed.text
        paths = {row["path"] for row in listed.json()["entries"]}
        assert "AGENT.md" in paths
        assert "experts/root.md" in paths
        assert "experts/variant.md" in paths

        read = client.get(
            "/v1/agent-blueprints/earthscope-flat/files/read",
            params={"session_id": sid, "path": "experts/root.md"},
        )
        assert read.status_code == 200, read.text
        assert "Coordinate genomics work." in read.text

        # A session_id whose ACTIVE blueprint id does not match the requested
        # id must NOT leak that active path -- falls back to catalog resolution
        # (404 here, since "genomics" was never installed either).
        mismatched = client.get("/v1/agent-blueprints/genomics/files", params={"session_id": sid})
        assert mismatched.status_code == 404, mismatched.text


# --------------------------------------------------------------------------- #
# Install-ALL registry semantics (owner ruling 2026-08-13): the marketplace is #
# the shipped standard library — every valid pack on main installs, first-run  #
# and as the registry gains packs; one broken pack never vetoes the set.       #
# --------------------------------------------------------------------------- #
_EXTRA_PACK_MD = """---
id: extra-pack
title: Extra Pack
version: 0.1.0
description: A second registry pack that must install by default.
---

A minimal single-agent pack.
"""

_BROKEN_PACK_MD = """---
title: No Id Pack
version: 0.1.0
---

Missing the required id field.
"""


def _make_multi_pack_registry_repo(path: Path) -> str:
    """A git registry (branch main) with the default pack + a second pack as subdirs."""

    default_dir = path / DEFAULT_AGENT_BLUEPRINT_ID
    _write_blueprint_tree(
        default_dir, main_md=_REACT_MAIN_MD, child_md=_REACT_CHILD_MD, commit="multi-head"
    )
    default_dir.joinpath(".clio-install.md").unlink()
    extra_dir = path / "extra-pack"
    extra_dir.mkdir(parents=True, exist_ok=True)
    extra_dir.joinpath("AGENT.md").write_text(_EXTRA_PACK_MD, encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "clio-test",
        "GIT_AUTHOR_EMAIL": "clio-test@example.com",
        "GIT_COMMITTER_NAME": "clio-test",
        "GIT_COMMITTER_EMAIL": "clio-test@example.com",
    }
    subprocess.run(
        ["git", "init", "-b", "main", str(path)], check=True, env=env, capture_output=True
    )
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, env=env, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "multi-pack registry"],
        check=True,
        env=env,
        capture_output=True,
    )
    return path.as_uri()


def test_first_run_bootstrap_installs_every_registry_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First-run bootstrap installs ALL registry packs, not only the default id.

    **Sabotage:** pass ``blueprint_id=DEFAULT_AGENT_BLUEPRINT_ID`` in the
    bootstrap install again -> extra-pack never installs -> red.
    """

    registry_url = _make_multi_pack_registry_repo(tmp_path / "registry")
    install_root, home = _prepare_default_store(tmp_path, monkeypatch, registry_url=registry_url)
    diagnostic = ensure_default_registry_bootstrap(home=home, cwd=tmp_path / "cwd")
    assert diagnostic == ""
    assert install_root.joinpath("AGENT.md").exists()
    assert install_root.parent.joinpath("extra-pack", "AGENT.md").exists()


def test_bootstrap_installs_pack_added_to_local_registry_after_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local-path registry that gains a pack installs it on the next bootstrap.

    The ``earthscope-flat`` case: the pack landed on marketplace main a month
    after the box's one-shot enumeration and was never installed.
    **Sabotage:** drop the local sync from the bootstrap -> the default is
    present so only the S4b evaluate runs -> the new pack never installs -> red.
    """

    registry_dir = tmp_path / "local-registry"
    default_dir = registry_dir / DEFAULT_AGENT_BLUEPRINT_ID
    _write_blueprint_tree(
        default_dir, main_md=_REACT_MAIN_MD, child_md=_REACT_CHILD_MD, commit="local-head"
    )
    default_dir.joinpath(".clio-install.md").unlink()
    install_root, home = _prepare_default_store(
        tmp_path, monkeypatch, registry_url=registry_dir.as_posix()
    )
    assert ensure_default_registry_bootstrap(home=home, cwd=tmp_path / "cwd") == ""
    assert install_root.joinpath("AGENT.md").exists()
    late_dir = registry_dir / "late-pack"
    late_dir.mkdir(parents=True)
    late_dir.joinpath("AGENT.md").write_text(
        _EXTRA_PACK_MD.replace("extra-pack", "late-pack"), encoding="utf-8"
    )
    # The sync is once-per-process (boot semantics); a new pack lands on the
    # NEXT boot — simulated by the test-only gate reset.
    from clio_agent.gact.agent_blueprint_refresh import reset_registry_sync_for_tests

    reset_registry_sync_for_tests()
    assert ensure_default_registry_bootstrap(home=home, cwd=tmp_path / "cwd") == ""
    assert install_root.parent.joinpath("late-pack", "AGENT.md").exists()


def test_uninstalled_pack_is_not_resurrected_by_the_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user uninstall is durable: the sync skips tombstoned ids until an
    explicit reinstall clears the tombstone.

    **Sabotage:** drop the tombstone check from the sync -> the pack reappears
    on the next boot -> red (the review 2026-08-13 blocker).
    """

    from clio_agent.gact.agent_blueprint_refresh import reset_registry_sync_for_tests
    from clio_agent.gact.agent_blueprints import (
        install_agent_blueprint,
        uninstall_agent_blueprint,
    )

    registry_dir = tmp_path / "local-registry"
    default_dir = registry_dir / DEFAULT_AGENT_BLUEPRINT_ID
    _write_blueprint_tree(
        default_dir, main_md=_REACT_MAIN_MD, child_md=_REACT_CHILD_MD, commit="ts-head"
    )
    default_dir.joinpath(".clio-install.md").unlink()
    extra_dir = registry_dir / "extra-pack"
    extra_dir.mkdir(parents=True)
    extra_dir.joinpath("AGENT.md").write_text(_EXTRA_PACK_MD, encoding="utf-8")
    install_root, home = _prepare_default_store(
        tmp_path, monkeypatch, registry_url=registry_dir.as_posix()
    )
    cwd = tmp_path / "cwd"
    assert ensure_default_registry_bootstrap(home=home, cwd=cwd) == ""
    extra_install = install_root.parent / "extra-pack"
    assert extra_install.joinpath("AGENT.md").exists()

    uninstall_agent_blueprint(blueprint_id="extra-pack", scope="global", cwd=cwd, home=home)
    reset_registry_sync_for_tests()
    assert ensure_default_registry_bootstrap(home=home, cwd=cwd) == ""
    assert not extra_install.exists(), "sync must not resurrect a user-uninstalled pack"

    install_agent_blueprint(
        source=registry_dir.as_posix(),
        scope="global",
        cwd=cwd,
        home=home,
        blueprint_id="extra-pack",
    )
    assert extra_install.joinpath("AGENT.md").exists()
    reset_registry_sync_for_tests()
    assert ensure_default_registry_bootstrap(home=home, cwd=cwd) == ""
    assert extra_install.joinpath("AGENT.md").exists(), "explicit reinstall clears the tombstone"


def test_install_all_skips_invalid_pack_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``skip_invalid`` skips a broken pack and installs the rest; strict raises.

    **Sabotage:** raise on invalid regardless of ``skip_invalid`` -> red; or
    silently skip in strict mode -> the strict assertion goes red.
    """

    from clio_agent.gact.agent_blueprints import install_agent_blueprint

    source_dir = tmp_path / "mixed-registry"
    good = source_dir / "extra-pack"
    good.mkdir(parents=True)
    good.joinpath("AGENT.md").write_text(_EXTRA_PACK_MD, encoding="utf-8")
    broken = source_dir / "broken-pack"
    broken.mkdir(parents=True)
    broken.joinpath("AGENT.md").write_text(_BROKEN_PACK_MD, encoding="utf-8")

    result = install_agent_blueprint(
        source=str(source_dir),
        scope="global",
        cwd=tmp_path / "cwd",
        home=tmp_path / "home",
        skip_invalid=True,
    )
    assert [row["id"] for row in result["installed"]] == ["extra-pack"]
    assert [row["id"] for row in result["skipped"]] == ["broken-pack"]

    with pytest.raises(ValueError):
        install_agent_blueprint(
            source=str(source_dir),
            scope="global",
            cwd=tmp_path / "cwd2",
            home=tmp_path / "home2",
        )


def test_dead_local_path_sources_are_pruned_on_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loading the sources registry drops rows whose local path no longer exists.

    Heals the pytest-tmpdir pollution class (~100 dead genomics rows) and keeps
    URL rows untouched. **Sabotage:** return rows unpruned -> red.
    """

    from clio_agent.gact.routes.blueprints import _load_agent_blueprint_sources

    user_dir = tmp_path / "user"
    user_dir.mkdir()
    monkeypatch.setenv("CLIO_USER_DIR", str(user_dir))
    dead = tmp_path / "gone-fixture-dir"
    rows = {
        "sources": [
            {"id": "src_dead", "source": str(dead), "ref": ""},
            {
                "id": "src_url",
                "source": "https://github.com/iowarp/clio-agent-marketplace.git",
                "ref": "main",
            },
        ]
    }
    user_dir.joinpath("agent-blueprint-sources.json").write_text(json.dumps(rows), encoding="utf-8")
    loaded = _load_agent_blueprint_sources()
    assert [row["id"] for row in loaded] == ["src_url"]
    rewritten = json.loads(
        user_dir.joinpath("agent-blueprint-sources.json").read_text(encoding="utf-8")
    )
    assert [row["id"] for row in rewritten["sources"]] == ["src_url"]
