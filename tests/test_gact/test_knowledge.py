"""Real vault parsing, index refresh, source isolation and immutable note custody."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.gact.routes.connected_storage import register_connected_storage_routes
from clio_agent.gact.storage import knowledge_tools
from clio_agent.gact.storage.knowledge_index import KnowledgeIndex
from clio_agent.gact.storage.knowledge_service import KnowledgeService
from clio_agent.gact.storage.models import CreateSource
from clio_agent.gact.storage.obsidian import ObsidianSource
from clio_agent.gact.storage.service import StorageService
from clio_agent.gact.storage.vault_parser import parse_note, resolve_links


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / ".obsidian").mkdir(parents=True)
    (root / ".obsidian" / "private.json").write_text("secret")
    (root / "a.md").write_text(
        "---\naliases: [Alpha]\ntags: [science]\n---\n# Topic\nUnique text #physics\n[[b#Section]] ![[figure.png]] [[Missing]]\n\nParagraph ^block\n\n`[[Code]]`\n```md\n[[Fenced]]\n```",
        encoding="utf-8",
    )
    (root / "b.md").write_text("# Section\n[[Alpha]]\n", encoding="utf-8")
    (root / "figure.png").write_bytes(b"attachment")
    return root


def test_parser_resolves_authored_links_and_ambiguity() -> None:
    a, links, props = parse_note(
        "a.md",
        "---\naliases: A\n---\n[[same]] [[https://example.com]] [B](b.md#Topic) `[[no]]`\n\nText ^real\n```\ntext ^fake\n```",
        "1",
    )
    b, _, _ = parse_note("b.md", "# Topic", "2")
    x, _, _ = parse_note("x/same.md", "", "3")
    y, _, _ = parse_note("y/same.md", "", "4")
    result = resolve_links([a, b, x, y], links)
    assert props["aliases"] == "A"
    assert a.blocks == ["real"]
    assert {link.target for link in result} == {"same", "https://example.com", "b.md"}
    assert result[0].status == "ambiguous" and len(result[0].candidates) == 2
    assert next(link for link in result if link.target == "b.md").selector == "Topic"


def test_incremental_index_and_exclusions(tmp_path: Path, vault: Path) -> None:
    service = StorageService(tmp_path / "private", tmp_path / "auth" / "credentials.json")
    record = service.create(
        "w", CreateSource(provider="obsidian", root=str(vault), label="Research")
    )
    index = KnowledgeIndex(service.store.root)
    snapshot = index.snapshot(record)
    assert snapshot.total == 3
    assert all(not note.path.startswith(".") for note in snapshot.notes)
    assert len(index.snapshot(record, query="Unique text").notes) == 1
    assert {link.target for link in snapshot.links} == {"b.md", "figure.png", "Missing", "a.md"}
    assert index.snapshot(record, limit=1).next_offset == 1
    assert index.snapshot(record, focus="b.md").total == 2
    (vault / "b.md").write_text("# Changed\n[[Missing]]", encoding="utf-8")
    changed = index.snapshot(record)
    assert changed.revision != snapshot.revision
    assert index.snapshot(record, query="Changed").total == 1
    (vault / "b.md").unlink()
    assert index.snapshot(record).total == 2
    adapter = ObsidianSource(str(vault))
    with pytest.raises(PermissionError):
        adapter.apply(".obsidian/private.json", None, None)
    with pytest.raises(PermissionError):
        adapter.apply("a.md", None, None)


def test_extensionless_note_links_do_not_conflict_with_attachment_stems() -> None:
    """An extensionless wikilink names Markdown; attachments need their extension."""
    source, links, _ = parse_note("Index.md", "[[Recipe]] [[Recipe.png]] [[Only image]]", "1")
    recipe, _, _ = parse_note("Notes/Recipe.md", "# Recipe", "2")
    image = recipe.model_copy(update={"path": "Photos/Recipe.png", "kind": "attachment"})
    orphan = image.model_copy(update={"path": "Photos/Only image.png"})
    resolved = resolve_links([source, recipe, image, orphan], links)
    assert [(link.target, link.status) for link in resolved] == [
        ("Notes/Recipe.md", "resolved"),
        ("Photos/Recipe.png", "resolved"),
        ("Only image", "unresolved"),
    ]


def test_vault_root_links_do_not_fall_back_to_an_unrelated_in_scope_note() -> None:
    """An escaping vault-root path stays unresolved even if its basename exists locally."""
    plan, authored, _ = parse_note("Plan.md", "[[Research/Methods#Section]] [[Research/../a]]", "1")
    method, _, _ = parse_note("Methods.md", "# Section", "2")
    local, _, _ = parse_note("a.md", "# Local note, different from the enclosing vault's a.md", "3")
    links = resolve_links([plan, method, local], authored, vault_prefix="Research")
    assert links[0].target == "Methods.md" and links[0].selector == "Section"
    assert links[0].status == "resolved"
    assert links[1].target == "Research/../a" and links[1].status == "unresolved"


def test_graph_keeps_cross_page_links_without_sending_note_contents(
    tmp_path: Path, vault: Path
) -> None:
    """Global topology includes every note beyond the ordinary 200-item page."""
    for index in range(601):
        (vault / f"z{index:04d}.md").write_text(
            f"# Entry {index}\nPrivate note text\n", encoding="utf-8"
        )
    (vault / "a.md").write_text(
        "---\naliases: [Alpha]\n---\n# A\n[[z0600]] [[z0600#Entry]] [[b]]", encoding="utf-8"
    )
    storage = StorageService(tmp_path / "private", tmp_path / "auth" / "credentials.json")
    record = storage.create(
        "w", CreateSource(provider="obsidian", root=str(vault), label="Research")
    )
    index = KnowledgeIndex(storage.store.root)
    page = index.snapshot(record)
    assert len(page.notes) == 200 and page.truncated
    assert not any(link.target == "z0600.md" for link in page.links)
    graph = index.graph(record)
    assert graph.total == 603 and len(graph.notes) == 603 and not graph.truncated
    assert {(link.source, link.target) for link in graph.links} >= {
        ("a.md", "z0600.md"),
        ("b.md", "a.md"),
    }
    assert sum(link.target == "z0600.md" for link in graph.links) == 1
    assert all(not note.excerpt and not note.headings and not note.blocks for note in graph.notes)
    assert not any(note.path == "figure.png" for note in graph.notes)
    assert any(note.path == "figure.png" for note in index.graph(record, attachments=True).notes)
    assert {note.path for note in index.graph(record, focus="a.md").notes} == {
        "a.md",
        "b.md",
        "z0600.md",
    }
    assert index.graph(record, query="Private note text").total == 601
    record.connected = False
    with pytest.raises(PermissionError):
        index.graph(record)


def test_subfolder_source_confines_index_reader_links_and_edits(
    tmp_path: Path, vault: Path
) -> None:
    """Selecting a project folder grants no access to sibling notes or private settings."""
    project = vault / "Research"
    project.mkdir()
    (project / "Plan.md").write_text(
        "# Plan\n[[Methods]] [[Research/Methods#Section]] [Methods](/Research/Methods.md) "
        "[[a]] [[../b]] [[Research/../a]]",
        encoding="utf-8",
    )
    (project / "Methods.md").write_text("# Methods\nCalibration", encoding="utf-8")
    (project / ".private").mkdir()
    (project / ".private" / "secret.md").write_text("secret")
    storage = StorageService(tmp_path / "private", tmp_path / "auth" / "credentials.json")
    record = storage.create(
        "w",
        CreateSource(provider="obsidian", root=str(project), label="Research", mode="working_copy"),
    )
    knowledge = KnowledgeService(
        storage, ResourceStore(root=tmp_path / "resources", max_resource_bytes=4096)
    )
    snapshot = knowledge.index.snapshot(record)
    assert {note.path for note in snapshot.notes} == {"Plan.md", "Methods.md"}
    assert knowledge.index.snapshot(record, query="Unique text").total == 0
    resolved = [link for link in snapshot.links if link.status == "resolved"]
    assert len(resolved) == 3
    assert all(link.target == "Methods.md" for link in resolved)
    assert any(link.selector == "Section" for link in resolved)
    assert all(
        link.status == "unresolved" for link in snapshot.links if link.target != "Methods.md"
    )
    opened = knowledge.open("w", record.source.id, "Plan.md")
    assert opened["text"].startswith("# Plan")
    with pytest.raises(KeyError, match="a.md"):
        knowledge.open("w", record.source.id, "a.md")
    for path in ("../a.md", ".private/secret.md", "../.obsidian/private.json"):
        with pytest.raises((PermissionError, ValueError)):
            knowledge.open("w", record.source.id, path)
    with pytest.raises((PermissionError, ValueError)):
        knowledge.edit("w", record.source.id, "../a.md", "bad", opened["note"]["revision"])
    knowledge.edit("w", record.source.id, "Plan.md", "# Revised", opened["note"]["revision"])
    assert knowledge.open("w", record.source.id, "Plan.md")["text"] == "# Revised"
    assert (project / "Plan.md").read_text(encoding="utf-8").startswith("# Plan")
    assert (vault / "a.md").read_text(encoding="utf-8").startswith("---")


def test_subfolder_rejects_private_scope_and_accepts_plain_notes(
    tmp_path: Path, vault: Path
) -> None:
    """A selected source cannot expose Obsidian private state as its content root."""
    with pytest.raises(PermissionError, match="approved vault"):
        ObsidianSource(str(vault / ".obsidian"))
    plain = tmp_path / "plain-notes"
    plain.mkdir()
    (plain / "First.md").write_text("# First\n[[Second]]", encoding="utf-8")
    (plain / "Second.md").write_text("# Second", encoding="utf-8")
    adapter = ObsidianSource(str(plain))
    assert adapter.vault_root == plain
    assert [entry.path for entry in adapter.entries()] == ["First.md", "Second.md"]
    assert not (plain / ".obsidian").exists()


def test_live_http_read_only_custody_gating_and_revocation(
    tmp_path: Path, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = FastAPI()
    app.state.workspaces = {"w": SimpleNamespace(root_path=str(workspace))}
    app.state.sessions = {"s": SimpleNamespace(workspace_id="w")}
    app.state.resource_store = ResourceStore(root=tmp_path / "resources", max_resource_bytes=4096)
    register_connected_storage_routes(app)
    monkeypatch.setattr(knowledge_tools.context, "active_app", lambda: app)
    monkeypatch.setattr(knowledge_tools.context, "active_session_id", lambda: "s")
    assert knowledge_tools.build_knowledge_tools() == []
    before = {
        path.relative_to(vault): path.read_bytes() for path in vault.rglob("*") if path.is_file()
    }
    with TestClient(app) as client:
        response = client.post(
            "/v1/workspaces/w/sources",
            json={"provider": "obsidian", "root": str(vault), "label": "Vault"},
        )
        assert response.status_code == 201, response.text
        source = response.json()
        assert source["source_kind"] == "knowledge" and source["connection"] == "local_vault"
        prefix = f"/v1/workspaces/w/sources/{source['id']}"
        assert {tool.name for tool in knowledge_tools.build_knowledge_tools()} == {
            "knowledge_search",
            "knowledge_open",
            "knowledge_related",
        }
        graph = client.get(prefix + "/knowledge")
        overview = client.get(prefix + "/knowledge/graph")
        assert overview.status_code == 200
        assert {note["path"] for note in overview.json()["notes"]} == {"a.md", "b.md"}
        assert (
            client.get(prefix.replace("/w/", "/elsewhere/") + "/knowledge/graph").status_code == 404
        )
        assert graph.status_code == 200, graph.text
        note = next(row for row in graph.json()["notes"] if row["path"] == "a.md")
        opened = client.post(
            prefix + "/knowledge/open", json={"path": "a.md", "revision": note["revision"]}
        )
        assert opened.status_code == 200, opened.text
        captured = opened.json()
        resource = app.state.resource_store.get("w", captured["resource"]["id"])
        assert (
            resource and resource.connected_source and resource.connected_source["kind"] == "note"
        )
        assert resource.sha256 == note["revision"]
        with pytest.raises(PermissionError):
            knowledge_tools.knowledge_edit(source["id"], "a.md", "bad", note["revision"])
        with pytest.raises(PermissionError):
            knowledge_tools.knowledge_create(source["id"], "new.md", "denied")
        assert client.get(prefix.replace("/w/", "/elsewhere/") + "/knowledge").status_code == 404
        assert client.post(
            prefix + "/knowledge/open", json={"path": "../outside.md"}
        ).status_code in {404, 409}
        assert client.post(prefix + "/disconnect").status_code == 200
        assert knowledge_tools.build_knowledge_tools() == []
        with pytest.raises(ValueError, match="disconnected"):
            knowledge_tools.knowledge_search(source["id"])
        assert app.state.resource_store.get("w", resource.id) is not None
    assert {
        path.relative_to(vault): path.read_bytes() for path in vault.rglob("*") if path.is_file()
    } == before


def test_editable_vault_enables_creation_and_revocation_removes_write_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creation is available only with an approved editable source, including after revocation."""
    storage = StorageService(tmp_path / "sources", tmp_path / "auth" / "credentials.json")
    record = storage.create(
        "w", CreateSource(provider="obsidian", create_vault=True, label="New", mode="write_enabled")
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            sessions={"s": SimpleNamespace(workspace_id="w")},
            connected_storage=storage,
            knowledge=KnowledgeService(
                storage, ResourceStore(root=tmp_path / "resources", max_resource_bytes=4096)
            ),
        )
    )
    monkeypatch.setattr(knowledge_tools.context, "active_app", lambda: app)
    monkeypatch.setattr(knowledge_tools.context, "active_session_id", lambda: "s")
    assert {tool.name for tool in knowledge_tools.build_knowledge_tools()} >= {
        "knowledge_create",
        "knowledge_edit",
    }
    knowledge_tools.knowledge_create(record.source.id, "First.md", "# First")
    storage.set_link_access(record, "read_only")
    storage.store.put("source", record.source.id, record)
    assert {tool.name for tool in knowledge_tools.build_knowledge_tools()} == {
        "knowledge_open",
        "knowledge_search",
        "knowledge_related",
    }
    with pytest.raises(PermissionError):
        knowledge_tools.knowledge_create(record.source.id, "Second.md", "denied")


@pytest.mark.parametrize("mode", ["working_copy", "write_enabled"])
def test_note_edits_use_existing_review_and_conflict_boundary(
    tmp_path: Path, vault: Path, mode: str
) -> None:
    """Stage or publish real note bytes, preserve preimages and reject stale revisions."""
    storage = StorageService(tmp_path / "private", tmp_path / "auth" / "credentials.json")
    record = storage.create(
        "w",
        CreateSource.model_validate(
            {"provider": "obsidian", "root": str(vault), "label": "Vault", "mode": mode}
        ),
    )
    resources = ResourceStore(root=tmp_path / "resources", max_resource_bytes=4096)
    knowledge = KnowledgeService(storage, resources)
    original = (vault / "a.md").read_text(encoding="utf-8")
    opened = knowledge.open("w", record.source.id, "a.md")
    saved = knowledge.edit(
        "w", record.source.id, "a.md", "# New note\n[[b]]", opened["note"]["revision"]
    )
    assert saved["writes_propagate"] is (mode == "write_enabled")
    assert (vault / "a.md").read_text(encoding="utf-8") == (
        "# New note\n[[b]]" if mode == "write_enabled" else original
    )
    edited = knowledge.open("w", record.source.id, "a.md")
    assert edited["text"] == "# New note\n[[b]]"
    assert knowledge.index.snapshot(storage.get("w", record.source.id), query="New note").total == 1
    with pytest.raises(ValueError, match="changed"):
        knowledge.edit("w", record.source.id, "a.md", "wrong", opened["note"]["revision"])
    assert resources.get("w", opened["resource"]["id"]) is not None
    assert knowledge.open("w", record.source.id, "b.md")["text"].startswith("# Section")


def test_reader_captures_attachments_and_private_settings_stay_excluded(
    tmp_path: Path, vault: Path
) -> None:
    storage = StorageService(tmp_path / "private", tmp_path / "auth" / "credentials.json")
    record = storage.create("w", CreateSource(provider="obsidian", root=str(vault), label="Vault"))
    knowledge = KnowledgeService(
        storage, ResourceStore(root=tmp_path / "resources", max_resource_bytes=4096)
    )
    attachment = next(
        note for note in knowledge.index.snapshot(record).notes if note.path == "figure.png"
    )
    captured = knowledge.open("w", record.source.id, "figure.png", attachment.revision)
    assert captured["note"]["kind"] == "attachment" and captured["text"] == ""
    (vault / "figure.png").write_bytes(b"changed attachment")
    with pytest.raises(ValueError, match="changed"):
        knowledge.open("w", record.source.id, "figure.png", attachment.revision)
    current = next(
        note for note in knowledge.index.snapshot(record).notes if note.path == "figure.png"
    )
    updated = knowledge.open("w", record.source.id, "figure.png", current.revision)
    assert updated["resource"]["sha256"] != captured["resource"]["sha256"]
    with pytest.raises(PermissionError, match="approved vault"):
        knowledge.open("w", record.source.id, ".obsidian/private.json")


def test_reader_refreshes_changed_note_and_keeps_old_capture(tmp_path: Path, vault: Path) -> None:
    """An upstream update yields a new resource while old evidence stays readable."""
    storage = StorageService(tmp_path / "private", tmp_path / "auth" / "credentials.json")
    record = storage.create("w", CreateSource(provider="obsidian", root=str(vault), label="Vault"))
    resources = ResourceStore(root=tmp_path / "resources", max_resource_bytes=4096)
    knowledge = KnowledgeService(storage, resources)
    original = knowledge.open("w", record.source.id, "a.md")
    (vault / "a.md").write_text("# Upstream change\n[[b]]", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        knowledge.open("w", record.source.id, "a.md", original["note"]["revision"])
    updated = knowledge.open("w", record.source.id, "a.md")
    assert updated["text"] == "# Upstream change\n[[b]]"
    assert updated["resource"]["id"] != original["resource"]["id"]
    assert resources.get("w", original["resource"]["id"]) is not None


def test_generic_writes_cannot_bypass_vault_exclusions(tmp_path: Path, vault: Path) -> None:
    """The generic file tool applies knowledge policy before staging any bytes."""
    from clio_agent.gact.storage.source_filesystem import SourceFileSystem

    storage = StorageService(tmp_path / "private", tmp_path / "auth" / "credentials.json")
    record = storage.create(
        "w", CreateSource(provider="obsidian", root=str(vault), label="Vault", mode="working_copy")
    )
    knowledge = KnowledgeService(
        storage, ResourceStore(root=tmp_path / "resources", max_resource_bytes=4096)
    )
    opened = knowledge.open("w", record.source.id, "a.md")
    fs = SourceFileSystem(
        storage, "w", record.source.id, knowledge.resources, revision=opened["link_revision"]
    )
    for path in (".obsidian/private.json", "figure.png"):
        with pytest.raises(PermissionError):
            with fs.open(path, "wb") as writer:
                writer.write(b"denied")
    assert (vault / ".obsidian" / "private.json").read_text() == "secret"
    with fs.open("new.md", "wb") as writer:
        writer.write(b"# Staged creation\n[[a]]")
    assert not (vault / "new.md").exists()
    record = storage.get("w", record.source.id)
    assert knowledge.index.snapshot(record, query="Staged creation").total == 1
    assert knowledge.open("w", record.source.id, "new.md")["text"].startswith("# Staged creation")
