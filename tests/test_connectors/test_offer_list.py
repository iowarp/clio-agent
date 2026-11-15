"""The plugins the agent may offer: the person's ticks over the default list (OL-D1 to OL-D7)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from clio_agent.gact import connector_offer, offer_list, plugin_offer
from clio_agent.user_config_document import user_config_path
from tests.test_connectors.test_plugin_offer import app  # noqa: F401 - the shared fixture

ORDER = ["fixture-plain", "fixture-secret", "fixture-signin", "Alpha", "tariff-calc"]


@pytest.fixture
def capped(app: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    """Two listed by default; a new conversation with the default agent has ``already-here``."""

    monkeypatch.setenv("CLIO_PLUGINS_OFFER_LISTED", "2")

    def resolve(_ref: str, *, pin: bool = True, **_kw: Any) -> Any:
        assert pin is False  # the settings view never pins a resolution
        return SimpleNamespace(plugins=[{"id": "already-here"}])

    app.state.plugin_service.resolve = resolve
    return app


def _merge(update: dict[str, Any]) -> dict[str, Any]:
    """Add ``update`` to the per-test user config (its test knobs stay)."""

    path = user_config_path()
    document = (yaml.safe_load(path.read_text()) if path.is_file() else None) or {}
    document.update(update)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document))
    return document


def _ids(rows: list[dict[str, Any]]) -> list[str]:
    return [row["id"] for row in rows]


def test_the_view_lists_every_candidate_with_the_default_cap_marked(capped: Any) -> None:
    view = offer_list.view(capped)
    assert [e["id"] for e in view["entries"]] == ORDER
    assert [e["default_listed"] for e in view["entries"]] == [True, True, False, False, False]
    assert [e["listed"] for e in view["entries"]] == [True, True, False, False, False]
    assert (view["cap"], view["total"], view["listed_count"], view["customized"]) == (
        2,
        5,
        2,
        False,
    )
    plain, tariff = view["entries"][0], view["entries"][-1]
    assert plain["kind"] == "connector" and plain["key"] == "connector:fixture-plain"
    assert "logo_url" in plain and plain["enabled"] is False
    assert tariff["kind"] == "plugin" and tariff["key"] == "plugin:lab/tariff-calc"
    assert tariff["marketplace"] == "lab" and tariff["logo_url"] is None


def test_untick_inside_the_cap_and_tick_beyond_it(capped: Any) -> None:
    view = offer_list.update(
        capped, include=["plugin:lab/tariff-calc"], exclude=["connector:fixture-secret"]
    )
    assert [e["id"] for e in view["entries"] if e["listed"]] == ["fixture-plain", "tariff-calc"]
    assert view["listed_count"] == 2 and view["customized"] is True
    stored = yaml.safe_load(user_config_path().read_text())["plugins"]["offer"]
    assert stored == {
        "include": ["plugin:lab/tariff-calc"],
        "exclude": ["connector:fixture-secret"],
    }

    # The session facts follow, in candidate order; the footer counts the rest (OL-D11).
    rows, total, left_out = plugin_offer.offerable_facts(capped, "sess_a")
    assert _ids(rows) == ["fixture-plain", "tariff-calc"] and (total, left_out) == (5, 1)
    from clio_agent import injections

    variables = {"plugins": rows, "total": total, "left_out": left_out}
    text = injections.render("session_facts_plugins", variables)
    assert "(5, 2 shown)" in text and "Fixture Secret" not in text and "tariff-calc" in text
    assert text.rstrip().endswith(
        "3 more not listed here (1 of them left out by the person in Settings); "
        "offer_plugin with search or category finds them."
    )


def test_redundant_and_unknown_ticks_are_dropped_and_a_conflict_is_typed(capped: Any) -> None:
    view = offer_list.update(
        capped,
        include=["connector:fixture-plain", "plugin:lab/gone"],  # already listed; unknown
        exclude=["plugin:lab/Alpha", "connector:nope"],  # beyond the cap; unknown
    )
    assert view["customized"] is False and view["include"] == view["exclude"] == []
    assert "plugins" not in (yaml.safe_load(user_config_path().read_text()) or {})
    with pytest.raises(offer_list.OfferListError) as refused:
        offer_list.update(capped, include=["plugin:lab/Alpha"], exclude=["plugin:lab/Alpha"])
    assert refused.value.code == "offer_list_conflict"


def test_unknown_keys_on_read_are_ignored(capped: Any) -> None:
    _merge({"plugins": {"offer": {"include": ["plugin:old/gone"]}}})
    view = offer_list.view(capped)
    assert view["include"] == [] and view["listed_count"] == 2
    assert _ids(plugin_offer.offerable_facts(capped, "sess_a")[0]) == ORDER[:2]


def test_reset_restores_the_default_and_keeps_other_settings(capped: Any) -> None:
    before = _merge({"plugins": {"offer_listed": 2}})
    offer_list.update(capped, include=["plugin:lab/Alpha"], exclude=[])
    assert offer_list.view(capped)["listed_count"] == 3
    view = offer_list.reset(capped)
    assert view["listed_count"] == 2 and view["customized"] is False
    assert yaml.safe_load(user_config_path().read_text()) == before


def test_an_unreadable_config_lists_the_default_traced(
    capped: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = user_config_path()
    path.write_text(path.read_text() + "\nplugins: [unclosed\n")
    seen: list[str] = []
    monkeypatch.setattr(offer_list.trace, "event", lambda _c, fmt, *a: seen.append(fmt % a))
    assert _ids(plugin_offer.offerable_facts(capped, "sess_a")[0]) == ORDER[:2]
    assert any(offer_list.OVERLAY_UNREADABLE in line for line in seen)
    with pytest.raises(offer_list.OfferListError) as refused:
        offer_list.view(capped)
    assert refused.value.code == offer_list.OVERLAY_UNREADABLE and refused.value.status == 409


def test_offer_plugin_finds_the_unticked_marked_and_offers_one_named(
    capped: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    offer_list.update(capped, include=[], exclude=["connector:fixture-secret"])
    overview = asyncio.run(plugin_offer.offer_plugin(reason="list"))
    assert (overview["total"], overview["listed"], overview["left_out"]) == (5, 1, 1)
    assert "offerable" not in overview  # no full dump (OL-D11)
    found = asyncio.run(plugin_offer.offer_plugin(reason="find", search="fixture"))
    rows = {row["id"]: row for row in found["matches"]}
    assert rows["fixture-secret"]["left_out_by_person"] is True
    assert rows["fixture-secret"]["listed"] is False and rows["fixture-plain"]["listed"] is True
    assert "left_out_by_person" not in rows["fixture-plain"]
    assert "left 1 of these out of the list in Settings" in found["message"]

    offered: list[str] = []

    async def offer_connector(connector: str, reason: str) -> dict[str, Any]:
        offered.append(connector)
        return {"connector": connector}

    monkeypatch.setattr(connector_offer, "offer_connector", offer_connector)
    asyncio.run(plugin_offer.offer_plugin(plugin="Fixture Secret", reason="It is needed."))
    assert offered == ["fixture-secret"]


def test_the_search_note_marks_an_unticked_row(capped: Any) -> None:
    offer_list.update(capped, include=[], exclude=["connector:fixture-secret"])
    rows = plugin_offer.offerable_matching(capped, "sess_a", "fixture-secret and fixture-plain")
    note = plugin_offer.search_note(rows)
    assert (
        'Fixture Secret (connector, id "fixture-secret", left out by the person in Settings)'
        in note
    )
    assert 'Fixture Plain (connector, id "fixture-plain")' in note


def test_the_routes_get_put_and_reset(capped: Any) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from clio_agent.gact.routes.offer_list import ROUTE, register_offer_list_routes

    api = FastAPI()
    api.state = capped.state
    register_offer_list_routes(api)
    client = TestClient(api)
    assert client.get(ROUTE).json()["listed_count"] == 2
    body = {"include": ["plugin:lab/Alpha"], "exclude": ["connector:fixture-plain"]}
    changed = client.put(ROUTE, json=body).json()
    assert [e["id"] for e in changed["entries"] if e["listed"]] == ["fixture-secret", "Alpha"]
    bad = client.put(ROUTE, json={"include": "plugin:lab/Alpha"})
    assert bad.status_code == 422 and bad.json()["detail"]["error"]["error"] == "validation_error"
    assert client.delete(ROUTE).json()["customized"] is False
