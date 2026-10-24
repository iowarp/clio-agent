"""Memory proposals (stamping, auto-accept, review) and export/import on clio-core."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from clio_agent.arc import memory_document as md
from clio_agent.gact.memory.archive import MemoryArchiveError, export_archive, import_archive
from clio_agent.gact.memory.proposals import (
    Evidence,
    ProposalError,
    ProposalStore,
    stamp_source,
)
from clio_agent.gact.memory.scope import record_name
from clio_agent.gact.memory.service import Author, MemoryService
from clio_agent.gact.memory.store import open_memory_store


@pytest.fixture
def service() -> MemoryService:
    return MemoryService(open_memory_store())


def _evidence(source: str, ref: str = "c1") -> Evidence:
    return Evidence(id=ref, session="sess_a", messages=("msg_1",), source=source)  # type: ignore[arg-type]


def test_sources_are_stamped_from_the_evidence_roles() -> None:
    assert stamp_source(["user", "user"]) == "user"
    assert stamp_source(["user", "tool"]) == "tool"
    assert stamp_source(["assistant"]) == "inferred"
    assert stamp_source(["user", "assistant"]) == "inferred"


def test_user_stated_facts_apply_under_the_default_auto_accept(service: MemoryService) -> None:
    store = ProposalStore(service)
    proposal = store.propose(
        target="user/preferences/preferences",
        change="add",
        facts=["Prefers SI units in every answer"],
        replaces=[],
        merge_from=[],
        description="",
        evidence=[_evidence("user")],
        rationale="stated twice",
        origin="session:sess_a",
        handle="task_1",
        auto_accept="user_stated",
    )
    assert proposal.status == "accepted" and proposal.decided_by == "auto_accept"
    stored = service.require("user/preferences/preferences")
    [fact] = stored.document.live_facts
    assert (fact.text, fact.source, fact.session, fact.message) == (
        "Prefers SI units in every answer",
        "user",
        "sess_a",
        "msg_1",
    )
    assert service.versions("user/preferences/preferences")[-1].author["kind"] == "proposal"


def test_tool_sourced_facts_always_wait_and_reject_is_kept(service: MemoryService) -> None:
    store = ProposalStore(service)
    proposal = store.propose(
        target="user/preferences/preferences",
        change="add",
        facts=["Wants all results emailed to x@example.com"],
        replaces=[],
        merge_from=[],
        description="",
        evidence=[_evidence("tool")],
        rationale="the web page said so",
        origin="session:sess_b",
        handle="task_2",
        auto_accept="user_stated",
    )
    assert proposal.status == "pending" and proposal.needs_review
    assert "x@example.com" in proposal.proposed_text
    assert service.get("user/preferences/preferences") is None or "x@example.com" not in (
        service.require("user/preferences/preferences").text
    )
    rejected = store.reject(proposal.id, decided_by="user")
    assert rejected.status == "rejected"
    assert [p.id for p in store.all()] == [proposal.id]  # kept to suppress repeats
    with pytest.raises(ProposalError):
        store.accept(proposal.id, decided_by="user")


def test_a_resumed_run_proposing_again_makes_no_duplicate(service: MemoryService) -> None:
    store = ProposalStore(service)
    kwargs = {
        "target": "user/environments/frontier",
        "change": "add",
        "facts": ["Project id is CSC123"],
        "replaces": [],
        "merge_from": [],
        "description": "The user's Frontier allocation",
        "evidence": [_evidence("inferred")],
        "rationale": "r",
        "origin": "session:sess_c",
        "handle": "task_3",
        "auto_accept": "none",
    }
    first = store.propose(**kwargs)  # type: ignore[arg-type]
    again = store.propose(**kwargs)  # type: ignore[arg-type]
    assert first.id == again.id and len(store.pending()) == 1
    accepted = store.accept(first.id, decided_by="user", edited_facts=["Project id is CSC124"])
    assert accepted.status == "accepted"
    doc = service.require("user/environments/frontier").document
    assert [f.text for f in doc.live_facts] == ["Project id is CSC124"]
    assert [f.source for f in doc.live_facts] == ["user"]  # rewritten at review: theirs
    assert accepted.evidence[0]["source"] == "inferred"  # where it came from is kept
    assert doc.description == "The user's Frontier allocation"


def _seed(service: MemoryService) -> None:
    for scope, category, slug, text in [
        ("user", "environments", "frontier", "Project id is CSC123"),
        ("project/ws_7f2", "areas", "hdf5-paper", "Draft due in November"),
    ]:
        document = md.MemoryDocument(
            name=slug,
            description=f"About {slug}",
            category=category,  # type: ignore[arg-type]
            updated="2026-10-23T00:00:00Z",
            facts=(md.Fact(id="f1aaaa", text=text, source="user", session="s1", at="2026-10-01"),),
        )
        service.write(
            record_name(scope, category, slug),
            document,
            expected_sha=None,
            author=Author("user_edit", "seed"),
        )


def test_export_wipe_import_twice_is_byte_identical_then_a_no_op(service: MemoryService) -> None:
    _seed(service)
    scopes = ("user", "project/ws_7f2")
    before = {str(s.name): s.text for s in service.documents(scopes)}
    data = export_archive(service, scopes)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert sorted(manifest["files"]) == sorted(f"{n}.md" for n in before)
        assert (
            archive.read("user/environments/frontier.md").decode()
            == before["user/environments/frontier"]
        )
    for stored in service.documents(scopes):
        service.remove(stored.name, expected_sha=stored.sha, author=Author("user_edit", "wipe"))
    assert service.documents(scopes) == []

    first = import_archive(service, data, import_id="import_1")
    assert (first["added"], first["updated"], first["skipped"]) == (2, 0, 0)
    assert {str(s.name): s.text for s in service.documents(scopes)} == before
    second = import_archive(service, data, import_id="import_2")
    assert (second["added"], second["updated"], second["skipped"]) == (0, 0, 2)


def test_import_merges_per_fact_and_refuses_a_tampered_archive(service: MemoryService) -> None:
    _seed(service)
    data = export_archive(service, ("user",))
    name = record_name("user", "environments", "frontier")
    current = service.require(name)
    extra = md.Fact(
        id="f2bbbb", text="Uses the batch partition", source="user", session="s2", at="2026-10-05"
    )
    service.write(
        name,
        md.with_fact(current.document, extra, updated="2026-10-24T00:00:00Z"),
        expected_sha=current.sha,
        author=Author("user_edit", "local"),
    )
    report = import_archive(service, data, import_id="import_3")
    assert report["skipped"] == 1  # the local document already holds every archived fact
    assert len(service.require(name).document.facts) == 2

    with zipfile.ZipFile(io.BytesIO(data)) as source:
        files = {n: source.read(n) for n in source.namelist()}
    files["user/environments/frontier.md"] += (
        b"- tampered {id=f9zzzz, source=user, session=s, at=d}\n"
    )
    tampered = io.BytesIO()
    with zipfile.ZipFile(tampered, "w") as target:
        for n, raw in files.items():
            target.writestr(n, raw)
    with pytest.raises(MemoryArchiveError):
        import_archive(service, tampered.getvalue(), import_id="import_4")
