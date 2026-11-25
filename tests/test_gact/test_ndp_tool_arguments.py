"""Every NDP tool takes every argument its function documents.

The tools are registered with explicit argument schemas. connected_data_search_ndp's
listed four of seven, so the documented paging call (dataset_id, resource_offset) was
refused as "Arg dataset_id is not in the tool's args" and a run could not reach a
lidar grid's remaining files.
"""

from __future__ import annotations

import inspect

import pytest

from clio_agent.gact.storage.ndp_endpoint_tools import build_ndp_endpoint_tools
from clio_agent.gact.storage.ndp_tools import build_ndp_discovery_tools


@pytest.mark.parametrize(
    "tool", build_ndp_discovery_tools() + build_ndp_endpoint_tools(), ids=lambda t: t.name
)
def test_a_tool_takes_its_functions_arguments(tool: object) -> None:
    func = tool.func
    target = inspect.unwrap(func)
    params = {
        name
        for name, p in inspect.signature(target).parameters.items()
        if p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)
    }
    assert params <= set(tool.args), sorted(params - set(tool.args))
