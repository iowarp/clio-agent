---
id: streaming.streams
section: capabilities
when: {tools: [open_stream]}
---
A stream is a named artifact whose newest entries (records, text lines or
bytes) live in clio-core. A script that watches something writes its state
there as often as it changes (`s = await stream.open(name)`, then
`await s.write(...)` in `run_code`, usually with `background=True`), with no
tool call per write. The stream shows in the side panel, live while open and
closed after; a dashboard chart or table whose `dataUri` is its `view_uri`
updates by itself while someone has it open. `read_stream` gives you its
state and newest entries when asked how things are going. A file that a script rewrites now and then is different:
register it with `create_artifact`, and a view bound to it at `@latest`
follows its versions.
