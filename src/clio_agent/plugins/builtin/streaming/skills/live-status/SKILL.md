---
name: live-status
description: Watch something that keeps changing (a job, an instrument, a device, a service) and show its status live. Use when the person asks to keep an eye on something, to monitor it, or to show how it is doing over time.
---

# Live status

The work is a background script that reads the source and writes what it sees
into a stream. The stream shows in the side panel by itself, live while it is
open; a dashboard bound to it gives a custom view.

## 1. Find how to read the source

Read the source once by hand first: a status endpoint, a command, or a file.
Note the fields that answer "how is it doing" (state, progress, temperatures,
errors). Keep the fields that change; constants belong in the dashboard text.

## 2. Start the watcher

Run it with `run_code(background=True, label=...)`. One stream holds the
current state: write each reading as a dict (one record per reading), or a
line of text for a log. Give it a `window` when the history matters, for
example the last few hundred readings for a chart.

```python
s = await stream.open("printer", window=300)
while True:
    status = await read_status()            # however this source is read
    await s.write(t=now_iso(), **status)
    if status["state"] in ("error", "done"):
        await task.notify(f"Printer {status['state']}", data=status)
        if status["state"] == "done":
            break
    await asyncio.sleep(2)
```

Each write goes straight to the stream in clio-core. Wake the conversation
with `task.notify` only for what the person should hear about. When the script
ends, its stream closes: its entries stay, shown as closed.

## 3. Show it

The stream itself opens in the side panel with its newest entries. For a
custom view, build a dashboard with the create-dashboard skill: bind its
charts and tables to the stream's `view_uri` (the `artifact://…@latest` that
`open_stream` returned) as their `dataUri`. The view reads the entries as
they are; the chart's own spec reshapes them. Use a table of the latest
record for the current state and a chart over the window for the trend.

## 4. Answer from the stream

When asked how it is going, call `read_stream(name)`: its state and newest
entries (`read_stream()` lists the workspace's streams). Do not re-read the
source, and do not start a second watcher. Say when the stream closed if it
did.

## 5. Stop

To stop watching, cancel the watcher's task: that closes its streams and
keeps their entries, shown as closed. `stop_stream(name)` is for a stream no
script writes to; a script that writes to a closed stream fails.
