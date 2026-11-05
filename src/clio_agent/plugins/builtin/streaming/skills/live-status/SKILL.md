---
name: live-status
description: Watch something that keeps changing (a job, an instrument, a device, a service) and show its status live. Use when the person asks to keep an eye on something, to monitor it, or to show how it is doing over time.
---

# Live status

The work is a background script that reads the source and writes what it sees
into a stream, and a dashboard bound to that stream.

## 1. Find how to read the source

Read the source once by hand first: a status endpoint, a command, or a file.
Note the fields that answer "how is it doing" (state, progress, temperatures,
errors). Keep the fields that change; constants belong in the dashboard text.

## 2. Start the watcher

Run it with `run_code(background=True, label=...)`. One stream holds the
current state. Give it a `window` when the history matters to the view, for
example the last few hundred readings for a chart.

```python
s = await stream.open("printer", columns=["t", "state", "progress", "nozzle_c"], window=300)
while True:
    status = await read_status()            # however this source is read
    await s.write(t=now_iso(), **status)
    if status["state"] in ("error", "done"):
        await task.notify(f"Printer {status['state']}", data=status)
        if status["state"] == "done":
            break
    await asyncio.sleep(2)
```

Each write goes straight to the stream. Wake the conversation with
`task.notify` only for what the person should hear about. When the script
ends, its stream stops: the last rows stay shown, marked stopped.

## 3. Show it

Build the dashboard with the create-dashboard skill. Bind its charts and
tables to the stream's `view_uri` (the `artifact://…@latest` that
`open_stream` returned) as their `dataUri`. Use a table of the latest row for
the current state and a chart over the window for the trend. Publish it.

## 4. Answer from the stream

When asked how it is going, call `read_stream(name)`. Do not re-read the
source, and do not start a second watcher. Say when the stream stopped if it
did.

## 5. Stop

`stop_stream(name)` or cancelling the task ends the writes. Both keep the last
status.
