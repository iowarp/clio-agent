"""B6 (#980) item 2: fleet territory enforcement — the closure of #966 §4.

Each MCP-fleet server's declared territory (its workspace binding) IS its fence profile's
``write_roots`` — so an out-of-territory write is PREVENTED (a ``policy_violation``, B2) while an
in-territory write stays correlated-to-call. The prevention/detection mint is exhaustively
covered in ``test_sandbox_b2.py`` (EROFS/EACCES → prevented, in-root skip, no-window guard);
this file binds the remaining B6 closure invariants, all deterministic:

* territory = fence profile — the fleet ``write_roots`` include the workspace binding;
* anti-drift (#974.6) — the SAME ``effective_write_roots`` base the fence uses is the advisory
  ``allowed_roots``, so the two can never diverge;
* a B5 root grant OBSERVABLY widens the territory (the fence narrows/widens by DECISION, never
  fakes) — the granted root joins the fleet ``write_roots`` on the next spawn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.runtime.sandbox_roots import (
    PROFILE_FLEET,
    PROFILE_SHELL,
    clear_write_root_grants,
    effective_write_roots,
    register_write_root_grant,
)
from clio_agent.tools.file_policy import FileAccessPolicy


@pytest.fixture(autouse=True)
def _clear_grants():
    clear_write_root_grants()
    yield
    clear_write_root_grants()


def _policy(base: Path) -> FileAccessPolicy:
    return FileAccessPolicy.from_mapping({"CLIO_ALLOWED_ROOTS": str(base)})


def test_fleet_territory_includes_the_workspace_binding(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    roots = effective_write_roots(PROFILE_FLEET, policy=_policy(tmp_path), workspace_root=str(ws))
    # The declared territory (workspace binding) is part of the fence profile.
    assert ws in roots


def test_fleet_and_advisory_share_one_base_no_drift(tmp_path: Path) -> None:
    # #974.6: the advisory allowed_roots is the SAME base the fence territory derives from —
    # so the OS fence can never be NARROWER than what file_policy already permits.
    policy = _policy(tmp_path)
    roots = effective_write_roots(PROFILE_FLEET, policy=policy, workspace_root=str(tmp_path))
    for advisory in policy.allowed_roots:
        assert advisory in roots


def test_root_grant_widens_the_fleet_territory(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    external = tmp_path / "granted_external"
    external.mkdir()
    policy = _policy(ws)  # narrow advisory base: only the workspace
    before = effective_write_roots(PROFILE_FLEET, policy=policy, workspace_root=str(ws))
    # The external dir is NOT in the territory before the grant.
    assert external not in before
    # A B5 DECISION grant widens it (observably changes enforcement).
    register_write_root_grant(str(ws), str(external))
    after = effective_write_roots(PROFILE_FLEET, policy=policy, workspace_root=str(ws))
    # Sabotage: drop the granted_write_roots union in effective_write_roots → red.
    assert external in after


def test_grant_is_scoped_to_its_workspace(tmp_path: Path) -> None:
    ws_a = tmp_path / "a"
    ws_a.mkdir()
    ws_b = tmp_path / "b"
    ws_b.mkdir()
    external = tmp_path / "ext"
    external.mkdir()
    register_write_root_grant(str(ws_a), str(external))
    # The grant to workspace A must NOT widen workspace B's territory.
    roots_b = effective_write_roots(PROFILE_SHELL, policy=_policy(ws_b), workspace_root=str(ws_b))
    assert external not in roots_b
