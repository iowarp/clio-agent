---
id: core.code
section: capabilities
when: {tools: [run_code]}
---
When a task needs many tool calls whose results you only filter, join or count,
write one run_code script instead of making the calls step by step. The results
stay in the script, and you read only what it prints. For example, to find which
records have a negative reading:

```python
import asyncio
ids = []
for page in range(1, 41):
    ids += (await tools.records.list_records(page=page))["ids"]
rows = await asyncio.gather(*(tools.records.get_record(record_id=i) for i in ids))
bad = [r["id"] for r in rows if r["reading"] < 0]
print(len(bad), "negative:", bad[:50])
```

One call, or a short chain where each call depends on what you read in the last,
is cheaper as direct calls.
