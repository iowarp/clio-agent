"""Import-parity tests for the extracted async MCP executor."""

from clio_agent.tools import execution, mcp_executor


def test_executor_symbols_are_reexported_from_execution() -> None:
    """The legacy and focused modules expose identical executor symbols."""

    assert execution.AsyncMCPToolExecutor is mcp_executor.AsyncMCPToolExecutor
    assert execution.MCPClientProtocol is mcp_executor.MCPClientProtocol
    assert execution.ClientFactory is mcp_executor.ClientFactory
