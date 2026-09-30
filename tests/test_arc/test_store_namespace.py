"""A clio-core store namespace isolates its records; the empty one keeps the bare tags.

Runs on the suite's private clio-core daemon (no other store exists).
"""

from __future__ import annotations

import uuid

from clio_agent.arc.storage import make_arc_store


def test_namespaces_isolate_the_same_record_and_share_within_one() -> None:
    a, b = f"ns-{uuid.uuid4().hex[:8]}", f"ns-{uuid.uuid4().hex[:8]}"
    store_a = make_arc_store(backend="cte", namespace=a)
    store_b = make_arc_store(backend="cte", namespace=b)
    try:
        store_a.put("segments", "s1__agent", b"from-a")

        assert store_a.get("segments", "s1__agent") == b"from-a"
        assert store_b.get("segments", "s1__agent") is None
        assert make_arc_store(backend="cte", namespace=a).get("segments", "s1__agent") == b"from-a"
    finally:
        store_a.clear()
        store_b.clear()


def test_the_default_namespace_is_the_bare_kind_tag() -> None:
    store = make_arc_store(backend="cte", namespace="")
    assert store.tag("segments") == "segments"
    assert make_arc_store(backend="cte", namespace="t1").tag("segments") == "t1/segments"
