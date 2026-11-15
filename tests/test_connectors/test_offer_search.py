"""offer_plugin finds what the session facts do not list, by words or category (OL-D11)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from clio_agent.connectors import state
from clio_agent.gact import offer_list, offer_search, plugin_offer
from clio_agent.gact.plugin_offer_presentation import present, present_start
from tests.test_connectors.test_plugin_offer import RECORDS, _record, app  # noqa: F401


def _row(name: str, **fields: Any) -> dict[str, Any]:
    return {"kind": "plugin", "id": name.lower(), "ref": f"lab/{name.lower()}", "name": name,
            "summary": "", "category": "", "keywords": [], **fields}  # fmt: skip


def test_every_word_must_match_and_name_hits_come_first() -> None:
    rows = [
        _row("Quakes", summary="Earthquake catalogs and waveforms", keywords=["seismic"]),
        _row("Seismology", summary="Waveforms, stations and events"),
        _row("Ocean", summary="Tide gauges", category="Earth science"),
    ]
    assert [r["name"] for r in offer_search.matching(rows, ["waveforms"])] == [
        "Quakes",
        "Seismology",
    ]
    # A name or id hit ranks first; then the rest in candidate order.
    assert [r["name"] for r in offer_search.matching(rows, ["seism"])] == ["Seismology", "Quakes"]
    assert offer_search.matching(rows, ["waveforms", "tide"]) == []
    assert [r["name"] for r in offer_search.matching(rows, ["earth", "science"])] == ["Ocean"]
    assert offer_search.words_of("  Seismic,  WAVEFORMS ") == ["seismic", "waveforms"]


def test_category_is_exact_and_any_case_and_the_overview_counts_them() -> None:
    rows = [
        _row("A", category="Earth science"),
        _row("B", category="earth science"),
        _row("C", category="Research"),
        _row("D"),
    ]
    assert [r["name"] for r in offer_search.in_category(rows, "EARTH SCIENCE")] == ["A", "B"]
    assert offer_search.in_category(rows, "Earth") == []
    assert offer_search.categories(rows) == [
        {"category": "Earth science", "count": 1},
        {"category": "earth science", "count": 1},
        {"category": "Research", "count": 1},
        {"category": "", "count": 1},
    ]


def test_results_are_capped_with_more_and_marked() -> None:
    rows = [_row(f"Kit {n:02d}", summary="a kit server") for n in range(57)]
    listed = rows[:20]
    left_out = {"plugin:lab/kit 03"}
    found = offer_search.search_result(rows, listed, left_out, 20, search="kit")
    assert (found["count"], found["more"], len(found["matches"])) == (57, 37, 20)
    assert found["matches"][3]["left_out_by_person"] is True
    assert found["matches"][3]["listed"] is True  # listed is the caller's list
    assert "keywords" not in found["matches"][0] and "ref" not in found["matches"][0]
    assert '57 found for "kit"; the first 20 shown' in found["message"]
    none = offer_search.search_result(rows, listed, set(), 20, search="zebra")
    assert (
        none["matches"] == [] and none["message"] == 'Nothing the person can add matches "zebra".'
    )


@pytest.fixture
def grouped(app: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    """The fixture connectors in Discover groups; a plugin with keywords and a category."""

    shipped = dict(state.all_entries())
    groups = {"fixture-plain": "Research", "fixture-secret": "Earth science"}
    monkeypatch.setattr(
        state,
        "all_entries",
        lambda: {k: replace(v, group=groups.get(k, "")) for k, v in shipped.items()},
    )
    RECORDS.append(
        _record("quakes", "Earthquake catalogs.", keywords=("seismic",), category="Earth science")
    )
    monkeypatch.setenv("CLIO_PLUGINS_OFFER_LISTED", "2")
    yield app
    RECORDS.pop()


def test_offer_plugin_searches_every_candidate(grouped: Any) -> None:
    rows = plugin_offer.offerable(grouped, "sess_a")
    by_id = {row["id"]: row for row in rows}
    assert by_id["fixture-secret"]["category"] == "Earth science"
    assert by_id["quakes"]["category"] == "Earth science"
    assert by_id["quakes"]["keywords"] == ["seismic"]
    offer_list._store(offer_list.Overlay(exclude=frozenset({"connector:fixture-secret"})))
    found = asyncio.run(plugin_offer.offer_plugin(reason="r", category="earth science"))
    assert [
        (m["id"], m["listed"], m.get("left_out_by_person", False)) for m in found["matches"]
    ] == [
        ("fixture-secret", False, True),
        ("quakes", False, False),
    ]
    by_word = asyncio.run(plugin_offer.offer_plugin(reason="r", search="seismic"))
    assert [m["id"] for m in by_word["matches"]] == ["quakes"]
    overview = asyncio.run(plugin_offer.offer_plugin(reason="r"))
    assert {"category": "Earth science", "count": 2} in overview["categories"]
    assert overview["total"] == len(rows) and overview["left_out"] == 1
    missing = asyncio.run(plugin_offer.offer_plugin(reason="r", plugin="earthquake"))
    # Not a name: the closest by its words come back with the refusal.
    assert missing["error"] == "plugin_not_found" and [m["id"] for m in missing["matches"]] == [
        "quakes"
    ]


def test_the_rows_read_search_category_and_nothing() -> None:
    found = present(
        {"search": "seismic waveforms", "reason": "r"},
        None,
        {
            "search": "seismic waveforms",
            "count": 2,
            "more": 0,
            "message": "2 match",
            "matches": [
                {"kind": "connector", "name": "Seismology", "summary": "Waveforms"},
                {"kind": "plugin", "name": "quakes", "left_out_by_person": True},
            ],
        },
    )
    assert (found["action"], found["subject"]) == ("Search plugins", "query")
    blocks = {block["id"]: block for block in found["blocks"]}
    assert blocks["query"]["text"] == "seismic waveforms"
    assert blocks["matches"]["label"] == "Found (2)"
    assert blocks["matches"]["text"] == (
        "Seismology (Connector): Waveforms\nquakes (Plugin, left out in Settings)"
    )
    category = present({"category": "Earth science"}, None, {"category": "Earth science",
                       "count": 57, "more": 37, "matches": [{"kind": "plugin", "name": "x"}]})  # fmt: skip
    assert (category["action"], category["subject"]) == ("Plugins in", "category")
    assert category["blocks"][0]["text"] == "Earth science"
    assert category["blocks"][-1] == {"id": "more", "type": "text", "label": "Not shown",
                                      "text": "37 more"}  # fmt: skip
    nothing = present({"search": "zebra"}, None, {"search": "zebra", "count": 0, "matches": []})
    assert nothing["blocks"][-1]["text"] == "Nothing matches"
    overview = present({}, None, {"total": 57, "categories": [
        {"category": "Earth science", "count": 5}, {"category": "", "count": 29}]})  # fmt: skip
    assert overview["action"] == "List"
    assert overview["blocks"][-1]["label"] == "Categories (57 in all)"
    assert overview["blocks"][-1]["text"] == "Earth science: 5\nNo category: 29"
    started = present_start({"search": "seismic", "reason": "r"})
    assert (started["action"], started["blocks"][0]["text"]) == ("Search plugins", "seismic")
    assert present_start({"category": "Research"})["action"] == "Plugins in"


def test_uncategorized_is_a_category_the_facts_name() -> None:
    """The session facts count entries with no category as "uncategorized" (OL-F4), and
    offer_plugin's category takes that name back."""

    rows = [_row("A", category="Research"), _row("B"), _row("C")]
    assert [r["name"] for r in offer_search.in_category(rows, "Uncategorized")] == ["B", "C"]
    found = offer_search.search_result(rows, [], set(), 20, category=offer_search.UNCATEGORIZED)
    assert found["count"] == 2
    from clio_agent import injections

    variables = {"plugins": [], "total": 3, "left_out": 0}
    variables["unlisted"] = offer_search.categories(rows)
    text = injections.render("session_facts_plugins", variables)
    assert f"3 more not listed here (Research 1; {offer_search.UNCATEGORIZED} 2);" in text


def test_the_unlisted_are_named_by_category_and_the_left_out_only_counted() -> None:
    """OL-F4: past the listed rows the facts name what a search would reach, by category;
    an entry the person left out is counted and never named."""

    rows = [
        _row("Geo", category="Earth science"),
        _row("Quakes", category="Earth science"),
        _row("Slurm", category="HPC"),
        _row("loose"),
    ]
    found = offer_search.unlisted(rows, {"plugin:lab/quakes"})
    assert found == [
        {"category": "Earth science", "count": 2, "names": ["Geo"]},
        {"category": "HPC", "count": 1, "names": ["Slurm"]},
        {"category": "", "count": 1, "names": ["loose"]},
    ]
    from clio_agent import injections

    text = injections.render(
        "session_facts_plugins", {"plugins": [], "total": 4, "left_out": 1, "unlisted": found}
    )
    assert (
        "4 more not listed here (Earth science 2: Geo; HPC 1: Slurm; uncategorized 1: loose);"
        in text
    )
    assert "Quakes" not in text


def test_the_agent_is_told_where_the_list_is_chosen() -> None:
    """OL-F5: the search note, the footer, a search's message and the tool description name
    the real setting and that a left-out entry is still offered by name; the settings
    row's title in gact-tui is "Plugins <agent> can offer" under Extensions."""

    from pathlib import Path

    import clio_agent

    core = Path(clio_agent.__file__).parent / "plugins/builtin/core/ai.iowarp.clio/injections"
    template = (core / "session_facts_plugins.md").read_text(encoding="utf-8")
    assert offer_list.SETTINGS_PLACE == 'Settings → Extensions → "Plugins CLIO can offer"'
    assert offer_list.SETTINGS_PLACE in template
    tool = plugin_offer.build_offer_plugin_tool()
    assert offer_list.SETTINGS_PLACE in tool.desc
    assert "the unlisted too" in tool.desc and "still offered by its name" in tool.desc
    # FN-12: the card is how the agent asks; a suggestion in prose adds nothing.
    assert "The card is how you ask" in tool.desc and "asks the person nothing" in tool.desc
    assert offer_search.left_out_fact(2).startswith(
        f"2 of these are left out by the person in {offer_list.SETTINGS_PLACE}, where they "
        "can tick them again"
    )
