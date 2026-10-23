"""Long-term memory tools, and recall of past sessions' transcripts."""

from clio_agent.sdk import native_tool

# Long-term memory: search and read everywhere; writes in root conversations and forks.
memory_search = native_tool("memory_search")
memory_read = native_tool("memory_read")
create_memory = native_tool("create_memory")
update_memory = native_tool("update_memory")
forget_memory = native_tool("forget_memory")
# The background pass's curator proposes changes; it never writes memory itself.
propose_memory_change = native_tool("propose_memory_change")

# Recall of past sessions' transcripts.
session_search = native_tool("session_search")
session_read_summary = native_tool("session_read_summary")
session_read_context_frame = native_tool("session_read_context_frame")
