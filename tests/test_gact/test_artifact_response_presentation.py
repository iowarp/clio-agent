"""Final answers project deliverables; the evidence registry retains every version."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.minting import drain_turn_artifacts
from clio_agent.gact.artifacts.presentation import response_deliverables
from clio_agent.gact.artifacts.proposals import Proposal, promote_proposal
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.wire import (
    append_turn_child_resource_links,
    append_turn_resource_links,
    resource_link_part,
)
from tests.test_gact.test_artifacts_s3 import _make_app, _proposal_events
from tests.test_gact.test_document_artifacts import _workspace_session
from tests.test_gact.test_spawn_runtime_s4 import _rollup_app, _rollup_task


def test_response_keeps_latest_deliverable_and_all_evidence(tmp_path: Path) -> None:
    """Only v2 and the requested images/PDFs are attached; nothing is discarded."""
    app, session, arc = _make_app(tmp_path)
    outputs = [
        Proposal(name="garden.html", content="<p>draft</p>", kind="report"),
        Proposal(name="garden.html", content="<p>corrected</p>", kind="report"),
        Proposal(name="review.png", content="review", kind="image", purpose="verification"),
        Proposal(name="working.pdf", content="work", kind="report", purpose="intermediate"),
        Proposal(name="requested.pdf", content="%PDF-requested", kind="report"),
        Proposal(name="requested.png", content="requested image", kind="image"),
    ]
    for proposal in outputs:
        result = promote_proposal(
            app, session.id, proposal, workspace_id="ws1", turn_id="turn", agent_id="main"
        )
        assert result.accepted, result
    ledger = Mock()
    append_turn_resource_links(app, session.id, "turn", ledger)
    links = [call.args[0] for call in ledger.append_part.call_args_list]
    assert [(part.name, part.metadata["version"]) for part in links] == [
        ("garden.html", 2),
        ("requested.pdf", 1),
        ("requested.png", 1),
    ]
    record = get_registry(app).get("ws1", "garden.html")
    assert record is not None and len(record.versions) == 2
    assert sum(len(record.versions) for record in get_registry(app).all_records()) == 6
    assert [event.payload["purpose"] for event in _proposal_events(arc)] == [
        "deliverable",
        "deliverable",
        "verification",
        "intermediate",
        "deliverable",
        "deliverable",
    ]


def test_explicit_redesignation_can_publish_identical_review_bytes(tmp_path: Path) -> None:
    """Changing presentation intent does not falsify the immutable original producer."""
    app, session, _ = _make_app(tmp_path)
    first = promote_proposal(
        app,
        session.id,
        Proposal(name="plot.svg", content="<svg/>", kind="image", purpose="verification"),
        workspace_id="ws1",
        turn_id="first",
    )
    assert first.accepted and first.version is not None
    drain_turn_artifacts(app, session.id, "first")
    repeated = promote_proposal(
        app,
        session.id,
        Proposal(path=str(tmp_path / "plot.svg"), kind="image", purpose="deliverable"),
        workspace_id="ws1",
        turn_id="second",
    )
    assert repeated.accepted and not repeated.created
    assert repeated.version == first.version
    assert first.version.producer["purpose"] == "verification"
    ledger = Mock()
    append_turn_resource_links(app, session.id, "second", ledger)
    assert ledger.append_part.call_count == 1
    assert ledger.append_part.call_args.args[0].metadata["artifact_id"] == first.version.artifact_id


def test_invalid_purpose_rejects_before_writing(tmp_path: Path) -> None:
    """Unknown presentation values are repairable errors, not silently hidden files."""
    app, session, arc = _make_app(tmp_path)
    result = promote_proposal(
        app,
        session.id,
        Proposal(name="bad.txt", content="bad", purpose="scratch"),
        workspace_id="ws1",
    )
    assert not result.accepted and result.reason == "invalid_purpose"
    assert not (tmp_path / "bad.txt").exists()
    assert _proposal_events(arc)[0].payload["purpose"] == "scratch"


def test_same_named_child_outputs_retain_separate_causal_identities(tmp_path: Path) -> None:
    """Revisions collapse inside one actor/run; independent producers remain visible."""
    app, session, _ = _make_app(tmp_path)
    entries: list[dict[str, Any]] = []
    for owner, run, body in [
        ("child-a", "a", "first"),
        ("child-a", "a", "fixed"),
        ("child-b", "b", "independent"),
    ]:
        outcome = promote_proposal(
            app,
            session.id,
            Proposal(name="report.txt", content=body),
            workspace_id="ws1",
            turn_id=run,
            agent_id=owner,
        )
        assert outcome.accepted and outcome.version is not None
        entries.append({"workspace_id": "ws1", "name": "report.txt", "version": outcome.version})
    assert [entry["version"].version for entry in response_deliverables(entries)] == [2, 3]


def test_child_rollup_honors_explicit_publication_of_reused_review_bytes(tmp_path: Path) -> None:
    """A child's response can publish existing evidence without changing its origin."""
    app, session, _ = _make_app(tmp_path)
    version = promote_proposal(
        app,
        session.id,
        Proposal(name="plot.svg", content="<svg/>", purpose="verification"),
        workspace_id="ws1",
    ).version
    assert version is not None
    parent = _rollup_app(tmp_path / "parent")
    parent.state.artifact_registry = get_registry(app)
    _rollup_task(parent, parent_sid="parent", child_sid="child", parent_turn_id="turn")
    parent.state.messages = {
        "child": [
            SimpleNamespace(
                role="assistant",
                parts=[resource_link_part("ws1", "plot.svg", version, part_id="published")],
            )
        ]
    }
    ledger = Mock()
    ledger.snapshot.return_value = []
    append_turn_child_resource_links(parent, "parent", "turn", ledger)
    assert ledger.append_part.call_count == 1
    assert ledger.append_part.call_args.args[0].metadata["artifact_id"] == version.artifact_id
    assert version.producer["purpose"] == "verification"


@pytest.mark.usefixtures("host_agent_executor")
def test_registry_and_bytes_api_retains_hidden_evidence(tmp_path: Path) -> None:
    """The real HTTP registry and exact-version bytes expose all retained outputs."""
    root = tmp_path / "workspace"
    root.mkdir()
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        app = cast(FastAPI, client.app)
        workspace, session = _workspace_session(client, root)
        versions = []
        for text, purpose in [
            ("first", "deliverable"),
            ("fixed", "deliverable"),
            ("review", "verification"),
        ]:
            result = promote_proposal(
                app,
                session,
                Proposal(
                    name="result.txt" if purpose == "deliverable" else "review.txt",
                    content=text,
                    purpose=purpose,
                ),
                workspace_id=workspace,
                turn_id="review",
                agent_id="main",
            )
            assert result.accepted and result.version is not None
            versions.append(result.version)
        ledger = Mock()
        append_turn_resource_links(app, session, "review", ledger)
        assert ledger.append_part.call_count == 1
        assert (
            ledger.append_part.call_args.args[0].metadata["artifact_id"] == versions[1].artifact_id
        )
        listing = client.get(f"/v1/workspaces/{workspace}/artifacts").json()["artifacts"]
        assert sum(len(record["versions"]) for record in listing) == 3
        for version, expected in zip(versions, [b"first", b"fixed", b"review"], strict=True):
            assert client.get(f"/v1/artifacts/{version.artifact_id}/bytes").content == expected
