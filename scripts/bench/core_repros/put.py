"""put.py N [size]: write N acknowledged blobs to tag "t"; record which were acked."""

import json
import os
import sys

import iowarp_core  # noqa: F401  # isort:skip  (preloads the native libraries first)
import clio_cte_core_ext as c  # isort:skip

c.clio_init(c.RuntimeMode.kClient, False)
c.initialize_cte(os.environ["CLIO_SERVER_CONF"], c.PoolQuery.Dynamic())
cl = c.get_cte_client()
tid = c.Tag("t").GetTagId()
n, sz = int(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 4096
ok = [
    i
    for i in range(n)
    if cl.AsyncPutBlob(tid, f"k{i}", (f"{i:08d}" * sz)[:sz].encode()).wait(30) == 0
]
json.dump([ok, sz], open(os.environ.get("OKJSON") or os.path.join(os.environ["H"], "ok.json"), "w"))
print("acked", len(ok), "of", n)
