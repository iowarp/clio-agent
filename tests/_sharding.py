"""Split the suite into N deterministic shards (``--shard-id`` / ``--num-shards``).

CI runs each shard on its own runner (``.github/workflows/ci.yml``), each with its
own xdist workers, and combines the shards' coverage in a separate job before the
floor is checked. A test's shard is ``crc32(nodeid) % num_shards``: stable across
runs and machines, identical in every xdist worker (xdist requires all workers to
collect the same items), and spread evenly without a stored timing table.
"""

from __future__ import annotations

import zlib

import pytest


def addoption(parser: pytest.Parser) -> None:
    """Register ``--shard-id`` and ``--num-shards`` (call from ``pytest_addoption``)."""
    group = parser.getgroup("clio-sharding")
    group.addoption(
        "--num-shards",
        type=int,
        default=1,
        help="split the collected tests into this many deterministic shards",
    )
    group.addoption(
        "--shard-id",
        type=int,
        default=0,
        help="run only this shard (0-based, below --num-shards)",
    )


def select(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect every item outside this run's shard (``pytest_collection_modifyitems``)."""
    num_shards = int(config.getoption("num_shards"))
    shard_id = int(config.getoption("shard_id"))
    if num_shards < 1 or not 0 <= shard_id < num_shards:
        raise pytest.UsageError(
            f"--shard-id must be in [0, --num-shards); got {shard_id} of {num_shards}"
        )
    if num_shards == 1:
        return
    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        in_shard = zlib.crc32(item.nodeid.encode("utf-8")) % num_shards == shard_id
        (selected if in_shard else deselected).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = selected
