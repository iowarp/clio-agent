"""check.py: read back the acked blobs and count good / wrong / unreadable / missing."""

import json
import os

import iowarp_core  # noqa: F401  # isort:skip  (preloads the native libraries first)
import clio_cte_core_ext as c  # isort:skip

c.clio_init(c.RuntimeMode.kClient, False)
c.initialize_cte(os.environ["CLIO_SERVER_CONF"], c.PoolQuery.Dynamic())
t = c.Tag("t")
ok, sz = json.load(open(os.path.join(os.environ["H"], "ok.json")))
names = set(t.GetContainedBlobs())
good = wrong = unreadable = missing = 0
for i in ok:
    if f"k{i}" not in names:
        missing += 1
        continue
    try:
        b = bytes(t.GetBlob(f"k{i}", sz))
    except RuntimeError:
        unreadable += 1
        continue
    if b == (f"{i:08d}" * sz)[:sz].encode():
        good += 1
    else:
        wrong += 1
print({"good": good, "wrong": wrong, "unreadable": unreadable, "missing": missing})
