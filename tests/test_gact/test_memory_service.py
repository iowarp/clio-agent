"""The long-term memory service on clio-core: scoped reads, BM25, the locked write path."""

from __future__ import annotations

import threading

import pytest

from clio_agent.arc import memory_document as md
from clio_agent.gact.memory.scope import record_name, scopes_name_re, session_scopes
from clio_agent.gact.memory.service import Author, MemoryConflict, MemoryService
from clio_agent.gact.memory.store import DOCUMENTS, VERSIONS, memory_namespace, open_memory_store
from clio_agent.gact.memory.usage import UsageCounters, document_subject

AUTHOR = Author("tool_call", "call_1", session="sess_a")


def _doc(slug: str, category: str, *facts: str, description: str = "") -> md.MemoryDocument:
    return md.MemoryDocument(
        name=slug,
        description=description or f"About {slug.replace('-', ' ')}",
        category=category,  # type: ignore[arg-type]
        updated="2026-10-23T00:00:00Z",
        facts=tuple(
            md.Fact(id=f"f{i:04d}x", text=text, source="user", session="sess_a", at="2026-10-23")
            for i, text in enumerate(facts)
        ),
    )


@pytest.fixture
def service() -> MemoryService:
    return MemoryService(open_memory_store())


def test_the_memory_store_has_its_own_namespace_and_kinds(service: MemoryService) -> None:
    assert memory_namespace().endswith("memory")
    assert service.store.tag(DOCUMENTS) == f"{memory_namespace()}/memories"
    assert service.store._kinds == (
        "memories",
        "memory_versions",
        "memory_proposals",
        "memory_usage",
        "memory_creation_runs",
    )


def test_write_versions_and_bm25_search_across_scopes(service: MemoryService) -> None:
    frontier = record_name("user", "environments", "frontier-allocation")
    paper = record_name("project/ws_7f2", "areas", "hdf5-paper")
    other = record_name("project/ws_other", "areas", "grant")
    service.write(
        frontier,
        _doc(frontier.slug, "environments", "Project id is CSC123"),
        expected_sha=None,
        author=AUTHOR,
    )
    service.write(
        paper,
        _doc(paper.slug, "areas", "Chunking study due in November"),
        expected_sha=None,
        author=AUTHOR,
    )
    service.write(
        other,
        _doc(other.slug, "areas", "Project id CSC999 for the grant"),
        expected_sha=None,
        author=AUTHOR,
    )

    hits = service.search(session_scopes("ws_7f2"), "project id")
    assert [hit.name for hit in hits] == [str(frontier)]  # ws_other is out of scope
    assert hits[0].facts[0].text == "Project id is CSC123"
    only_project = service.search(session_scopes("ws_7f2", use_account_memory=False), "project")
    assert only_project == []
    assert [h.name for h in service.search(("user", "project/ws_7f2"), "chunking")] == [str(paper)]
    assert service.search(("user",), "chunking", category="environments") == []

    assert [str(s.name) for s in service.documents(("user", "project/ws_7f2"))] == [
        str(frontier),
        str(paper),
    ]
    history = service.versions(frontier)
    assert [(v.n, v.text, v.author["id"]) for v in history] == [(1, "", "call_1")]


def test_a_stale_writer_gets_memory_conflict_and_no_update_is_lost(
    service: MemoryService,
) -> None:
    name = record_name("user", "topics", "telescopes")
    first = service.write(
        name, _doc(name.slug, "topics", "Uses the VLA"), expected_sha=None, author=AUTHOR
    )
    results: list[object] = []

    def writer(text: str) -> None:
        try:
            current = service.require(name)
            doc = md.with_fact(
                current.document,
                md.Fact(id=md.new_fact_id(), text=text, source="user", session="s", at="d"),
                updated="2026-10-24T00:00:00Z",
            )
            results.append(service.write(name, doc, expected_sha=first.sha, author=AUTHOR))
        except MemoryConflict as conflict:
            results.append(conflict)

    threads = [threading.Thread(target=writer, args=(t,)) for t in ("Uses ALMA", "Uses JWST")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    conflicts = [r for r in results if isinstance(r, MemoryConflict)]
    assert len(conflicts) == 1
    assert conflicts[0].details["current_sha"] == service.require(name).sha
    # The loser retries on the new hash: both facts survive.
    current = service.require(name)
    retried = md.with_fact(
        current.document,
        md.Fact(id=md.new_fact_id(), text="retry", source="user", session="s", at="d"),
        updated="2026-10-25T00:00:00Z",
    )
    service.write(name, retried, expected_sha=current.sha, author=AUTHOR)
    assert len(service.require(name).document.live_facts) == 3


def test_a_shrunk_document_is_not_found_by_the_words_it_lost(service: MemoryService) -> None:
    name = record_name("user", "topics", "marine-life")
    long = _doc(
        name.slug,
        "topics",
        *[f"Observation {i} of reef fish" for i in range(200)],
        "Saw an octopus near the pier",
    )
    stored = service.write(name, long, expected_sha=None, author=AUTHOR)
    assert [h.name for h in service.search(("user",), "octopus")] == [str(name)]
    short = _doc(name.slug, "topics", "Likes reef fish")
    service.write(name, short, expected_sha=stored.sha, author=AUTHOR)
    assert service.search(("user",), "octopus") == []
    assert [h.name for h in service.search(("user",), "reef")] == [str(name)]


def test_remove_archives_and_restore_brings_a_version_back(service: MemoryService) -> None:
    name = record_name("user", "people", "ana-lopez")
    stored = service.write(
        name,
        _doc(name.slug, "people", "Co-author on the HDF5 paper"),
        expected_sha=None,
        author=AUTHOR,
    )
    service.remove(name, expected_sha=stored.sha, author=AUTHOR)
    assert service.get(name) is None
    assert service.search(("user",), "co-author") == []
    versions = service.versions(name)
    assert [v.n for v in versions] == [1, 2] and versions[-1].text == stored.text
    restored = service.restore(name, 2, author=Author("restore", "page"))
    assert restored.text == stored.text
    assert len(service.versions(name)) == 3


def test_forget_lists_fact_ids_on_the_scope_profile(service: MemoryService) -> None:
    service.forget("user", ("f1abcd",), author=AUTHOR)
    service.forget("user", ("f1abcd", "f2abcd"), author=AUTHOR)
    assert service.forgotten("user") == ("f1abcd", "f2abcd")
    assert service.forgotten("project/ws_7f2") == ()


def test_usage_counts_apply_at_turn_end_and_follow_ratings(service: MemoryService) -> None:
    usage = UsageCounters(service.store)
    subject = document_subject("user/topics/telescopes")
    usage.note("turn_1", subject, "loaded")
    usage.note("turn_1", subject, "read")
    assert usage.get(subject).read == 0  # nothing written before turn end
    usage.finish("turn_1", completed=True)
    counted = usage.get(subject)
    assert (counted.loaded, counted.read, counted.read_in_success) == (1, 1, 1)
    assert counted.last_read
    usage.rate((subject,), became_bad=True)
    assert usage.get(subject).read_in_success == 0
    assert service.store.get(VERSIONS, "anything") is None


def test_scope_regex_selects_exactly_the_session_scopes() -> None:
    import re

    pattern = re.compile(scopes_name_re(("user", "project/ws_7f2")))
    assert pattern.fullmatch("user/topics/a-b")
    assert pattern.fullmatch("project/ws_7f2/areas/x")
    assert not pattern.fullmatch("project/ws_7f20/areas/x")
    assert not pattern.fullmatch("project/ws_other/areas/x")


def test_search_follows_the_client_binding_not_a_later_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client binds its pool once, at attach; a test or tool that later edits the
    environment does not turn search off for stores opened afterwards."""
    monkeypatch.delenv("CLIO_CTE_POOL", raising=False)
    assert MemoryService(open_memory_store()).store.supports_search()
