"""The stream tools: open a stream, read its latest rows, stop it."""

from clio_agent.sdk import native_tool

open_stream = native_tool("open_stream")
read_stream = native_tool("read_stream")
stop_stream = native_tool("stop_stream")
