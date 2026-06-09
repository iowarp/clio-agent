"""Routing integration test: HDF5Expert should win HDF5-specific queries
without stealing general data-format questions from DataExpert.

The keyword scorer is crude (sum of len(kw)/10 / priority); these tests
codify the property we want even if the scoring formula changes later.
"""

from __future__ import annotations

import pytest

from clio_agent.experts import DataExpert, HDF5Expert
from clio_agent.registry import AgentCapability, AgentRegistry


@pytest.fixture
def registry():
    r = AgentRegistry()
    data_caps = DataExpert.get_capabilities()
    hdf5_caps = HDF5Expert.get_capabilities()
    r.register_agent(
        "data",
        DataExpert(),
        AgentCapability(
            keywords=list(data_caps["keywords"]),
            description=data_caps["description"],
            tools=[],
            specialization="data_io",
            priority=int(data_caps["priority"]),
        ),
    )
    r.register_agent(
        "hdf5",
        HDF5Expert(),
        AgentCapability(
            keywords=list(hdf5_caps["keywords"]),
            description=hdf5_caps["description"],
            tools=[],
            specialization="hdf5_domain",
            priority=int(hdf5_caps["priority"]),
        ),
    )
    return r


@pytest.mark.parametrize(
    "query",
    [
        "how do I set up SWMR for a streaming HDF5 file",
        "show me how to use the ros3 vfd to read from S3",
        "create a virtual dataset combining multiple HDF5 files",
        "is my NetCDF4 file CF compliant",
        "what's a good chunk size for parallel HDF5 with mpi-io",
        "implement a VOL connector for caching",
        "tune compression filter selection between gzip and shuffle",
    ],
)
def test_hdf5_specific_queries_route_to_hdf5(registry, query):
    decision = registry.route_query(query)
    assert decision.selected_agent == "hdf5", (
        f"query {query!r} routed to {decision.selected_agent} "
        f"(matched: {decision.matched_keywords})"
    )


@pytest.mark.parametrize(
    "query",
    [
        "should I use Parquet or HDF5 for columnar analytics",
        "best practices for parquet compression",
        "compare data formats for time-series ingestion",
    ],
)
def test_general_data_queries_stay_on_data_expert(registry, query):
    decision = registry.route_query(query)
    assert decision.selected_agent == "data", (
        f"query {query!r} routed to {decision.selected_agent} — DataExpert "
        f"should keep general data-format questions."
    )
