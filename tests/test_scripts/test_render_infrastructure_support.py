"""The managed-infrastructure support tables are the catalog's own answer, never prose."""

from __future__ import annotations

from scripts.render_infrastructure_support import BEGIN, DOC, END, PROFILES, render, splice


def test_published_support_tables_match_the_service_catalog() -> None:
    """A catalog change that alters what a host can run must regenerate the page."""
    current = DOC.read_text(encoding="utf-8")
    assert splice(current) == current, (
        "site/src/content/docs/docs/managed-infrastructure.mdx is out of date: "
        "run uv run python scripts/render_infrastructure_support.py"
    )


def test_every_host_profile_gets_a_table_inside_the_generated_markers() -> None:
    rendered = render()
    assert rendered.startswith(BEGIN) and rendered.endswith(END)
    assert all(f"### {heading}" in rendered for heading, *_ in PROFILES)
