---
id: streaming.streams
section: capabilities
when: {tools: [open_stream]}
---
A stream is a named table whose latest rows live in memory. A script that
watches something writes its state there as often as it changes
(`s = await stream.open(name)`, then `await s.write(...)` in `run_code`,
usually with `background=True`), with no tool call per write. A dashboard
chart or table whose `dataUri` is the stream's `view_uri` updates by itself
while someone has it open. `read_stream` gives you the latest rows when asked
how things are going. A file that a script rewrites now and then is different:
register it with `create_artifact`, and a view bound to it at `@latest`
follows its versions.
