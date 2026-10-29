"""A report must select a tab child, not a nested diagram that leaves the view blank."""

from __future__ import annotations

import pytest

from clio_agent.gact.a2ui_catalogs.builtin import load_builtin_catalogs
from clio_agent.gact.a2ui_catalogs.validation import (
    A2UIValidationError,
    validate_components,
    validate_value,
)


@pytest.mark.parametrize("active", ["inner_chart", "Overview", "missing"])
def test_invalid_static_tab_selection_names_the_allowed_children(active: str) -> None:
    with pytest.raises(A2UIValidationError, match=r"tabs\[\].child.*overview_panel"):
        validate_components(
            load_builtin_catalogs()[1],
            [
                {
                    "id": "views",
                    "component": "Tabs",
                    "activeTab": active,
                    "tabs": [{"title": "Overview", "child": "overview_panel"}],
                }
            ],
        )


@pytest.mark.parametrize("active", ["overview_panel", {"path": "/view/tab"}])
def test_static_child_and_declared_dynamic_selection_remain_valid(active: object) -> None:
    validate_components(
        load_builtin_catalogs()[1],
        [
            {
                "id": "views",
                "component": "Tabs",
                "activeTab": active,
                "tabs": [{"title": "Overview", "child": "overview_panel"}],
            }
        ],
    )


def test_arbitrary_context_data_is_not_interpreted_as_a_tab_component() -> None:
    validate_value(
        {"component": "Tabs", "activeTab": "source-data", "tabs": [{"child": "raw-value"}]},
        entry=None,
        max_depth=32,
        max_string=65536,
    )
