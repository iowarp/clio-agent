"""``search.*`` configuration: defaults, validation and the engine policy."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from clio_agent.search import settings as search_settings
from clio_agent.search.settings import (
    DEFAULT_ENGINES,
    SearchConfigurationError,
    build_settings,
    effective_engines,
    is_opt_in_only,
    load_search_settings,
)

CHINESE_ENGINES = ("baidu", "baidu images", "sogou", "sogou wechat", "360search", "chinaso news")
DEFAULTS_FILE = Path(search_settings.__file__).resolve().parents[1] / "config.defaults.yaml"


def test_web_search_defaults_to_the_private_searxng() -> None:
    settings = load_search_settings()
    assert settings.backend == "local_searxng"
    # The suite's config layer turns the boot install off; the product default is on.
    assert settings.auto_install is False and build_settings().auto_install is True
    assert settings.engines == DEFAULT_ENGINES
    assert settings.safe_search == 1
    assert settings.language == "en"
    assert 1 <= settings.request_timeout_s <= 60
    assert settings.max_results == 10


def test_defaults_prefer_reputable_general_and_scholarly_engines() -> None:
    for name in ("duckduckgo", "brave", "mojeek", "qwant", "startpage", "wikipedia"):
        assert name in DEFAULT_ENGINES
    for name in ("arxiv", "crossref", "semantic scholar", "pubmed"):
        assert name in DEFAULT_ENGINES
    assert not any(is_opt_in_only(name) for name in DEFAULT_ENGINES)


@pytest.mark.parametrize("engine", CHINESE_ENGINES)
def test_chinese_engines_are_off_unless_explicitly_opted_in(engine: str) -> None:
    enabled, dropped = effective_engines(["duckduckgo", engine])
    assert enabled == ["duckduckgo"] and dropped == [engine]
    settings = build_settings(engines=["duckduckgo", engine])
    assert engine not in settings.engines
    assert any("opt_in_engines" in note for note in settings.notes)
    opted = build_settings(engines=["duckduckgo", engine], opt_in_engines=[engine])
    assert engine in opted.engines and not opted.dropped_engines


def test_an_opt_in_alone_enables_the_engine() -> None:
    enabled, dropped = effective_engines(["duckduckgo"], ["baidu"])
    assert enabled == ["duckduckgo", "baidu"] and dropped == []


def test_the_environment_overrides_and_the_policy_still_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_SEARCH_BACKEND", "clio_web_search")
    monkeypatch.setenv("CLIO_SEARCH_CLIO_WEB_SEARCH_URL", "http://search-host:8089/")
    monkeypatch.setenv("CLIO_SEARCH_SEARXNG_ENGINES", "wikipedia,sogou")
    settings = load_search_settings()
    assert settings.backend == "clio_web_search"
    assert settings.clio_web_search_url == "http://search-host:8089"
    assert settings.engines == ("wikipedia",)
    assert settings.dropped_engines == ("sogou",)


@pytest.mark.parametrize(
    ("values", "match"),
    [
        ({"backend": "google"}, "search.backend"),
        ({"clio_web_search_url": "search-host:8089"}, "http"),
        ({"safe_search": 3}, "safe_search"),
        ({"request_timeout_s": 0.0}, "request_timeout_s"),
        ({"port": 80}, "port"),
        ({"engines": ["sogou"]}, "no engine"),
    ],
)
def test_invalid_values_are_typed_errors(values: dict, match: str) -> None:
    with pytest.raises(SearchConfigurationError, match=match):
        build_settings(**values)


def test_committed_defaults_match_the_in_code_defaults() -> None:
    document = yaml.safe_load(DEFAULTS_FILE.read_text(encoding="utf-8"))
    code = build_settings()
    assert document["search.backend"] == code.backend
    assert document["search.local_searxng.auto_install"] is code.auto_install
    assert tuple(document["search.searxng.engines"]) == code.engines
    assert document["search.searxng.port"] == code.port
    assert document["search.searxng.safe_search"] == code.safe_search
    assert document["search.searxng.request_timeout_s"] == code.request_timeout_s
    assert document["search.searxng.max_results"] == code.max_results
    assert document["search.searxng.language"] == code.language
    assert "search.clio_web_search.url" not in document
