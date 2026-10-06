"""Tests for the Altair spec pre-check bundled with present-interactive-analysis."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

import clio_agent.gact.skills as skills_module

_SCRIPT = (
    Path(skills_module.__file__).resolve().parent
    / "builtin_skills"
    / "present-interactive-analysis"
    / "scripts"
    / "vega_spec.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("vega_spec", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _named_source_spec() -> dict[str, object]:
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"name": "source"},
        "params": [{"name": "sel", "select": {"type": "point", "fields": ["entity"]}}],
        "mark": "line",
        "encoding": {"x": {"field": "t", "type": "temporal"}},
    }


def test_named_source_spec_passes() -> None:
    assert _load().check_spec(_named_source_spec()) == []


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda s: s.update(datasets={"data-1": [{"t": 1}]}), "datasets"),
        (lambda s: s.update(data={"values": [{"t": 1}]}), "exactly"),
        (lambda s: s.update(data={"name": "other"}), "exactly"),
        (lambda s: s["encoding"].update(href={"url": "http://x"}), "url"),
        (lambda s: s.update(usermeta={"a": 1}), "usermeta"),
        (lambda s: s.update(description="x" * 70_000), "limit"),
    ],
)
def test_refusal_causes_are_reported(mutate, needle: str) -> None:  # type: ignore[no-untyped-def]
    """Every refusal cause is still reported, now via the real server-side guard."""

    spec = _named_source_spec()
    mutate(spec)
    problems = _load().check_spec(spec)
    assert any(needle in problem for problem in problems), problems


def test_non_object_spec_is_rejected() -> None:
    assert _load().check_spec([1, 2]) == ["the spec must be a JSON object"]


def test_wrapped_facet_and_projection_specs_pass() -> None:
    """The guard's newer data-free layout keys (#1549 G4) reach this pre-check too."""

    check_spec = _load().check_spec
    facet_spec = {
        "data": {"name": "source"},
        "facet": {"field": "category", "type": "nominal"},
        "columns": 3,
        "spec": {
            "mark": "point",
            "encoding": {
                "x": {"field": "t", "type": "quantitative"},
                "y": {"field": "v", "type": "quantitative"},
            },
        },
    }
    assert check_spec(facet_spec) == []

    point_map_spec = {
        "data": {"name": "source"},
        "mark": "circle",
        "encoding": {
            "latitude": {"field": "lat", "type": "quantitative"},
            "longitude": {"field": "lon", "type": "quantitative"},
        },
        "projection": {"type": "equirectangular"},
    }
    assert check_spec(point_map_spec) == []


def test_url_inside_an_otherwise_allowed_projection_spec_is_still_refused() -> None:
    """A newly-allowed top-level key (``projection``) doesn't create a new url loophole."""

    spec = {
        "data": {"name": "source"},
        "mark": "circle",
        "encoding": {"latitude": {"field": "lat", "type": "quantitative"}},
        "projection": {"type": "equirectangular", "url": "http://evil.example/tiles"},
    }
    problems = _load().check_spec(spec)
    assert any("url" in problem for problem in problems), problems


def test_check_command_exit_codes(tmp_path: Path) -> None:
    module = _load()
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_named_source_spec()), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**_named_source_spec(), "datasets": {}}), encoding="utf-8")
    assert module.main(["check", str(good)]) == 0
    assert module.main(["check", str(bad)]) == 1


def test_build_requires_a_chart_variable(tmp_path: Path) -> None:
    chart_file = tmp_path / "chart.py"
    chart_file.write_text("x = 1\n", encoding="utf-8")
    assert _load().main(["build", str(chart_file), str(tmp_path / "out.json")]) == 2
