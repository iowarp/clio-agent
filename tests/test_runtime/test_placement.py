"""Placement seam: role-queue default + node-affinity, via the factory (epic #667/#659)."""

from __future__ import annotations

import pytest

from clio_agent.runtime.placement import (
    NodeAffinityPlacement,
    Placement,
    RoleQueuePlacement,
    make_placement,
)


def test_role_queue_is_the_default_and_ignores_hints():
    p = make_placement("role")
    assert isinstance(p, RoleQueuePlacement) and isinstance(p, Placement)
    assert p.mailbox_for("analysis") == "clio_core_analysis"
    assert p.mailbox_for("data", hints={"node": "n2"}) == "clio_core_data"  # caller decoupled from node


def test_node_affinity_routes_to_a_node_queue_when_hinted():
    p = make_placement("affinity")
    assert p.mailbox_for("data") == "clio_core_data"  # no hint -> plain role queue
    assert p.mailbox_for("data", hints={"node": "n2"}) == "clio_core_n2_data"  # "run where the data is"


def test_factory_reads_env_and_rejects_unknown(monkeypatch):
    monkeypatch.setenv("CLIO_PLACEMENT", "affinity")
    assert isinstance(make_placement(), NodeAffinityPlacement)
    with pytest.raises(ValueError):
        make_placement("nope")


def test_empty_role_rejected():
    for p in (make_placement("role"), make_placement("affinity")):
        with pytest.raises(ValueError):
            p.mailbox_for("")
