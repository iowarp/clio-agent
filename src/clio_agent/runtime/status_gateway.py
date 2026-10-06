"""Gateway capability enumeration for the runtime doctor."""

from typing import Any


def _list_gateway_capabilities() -> list[dict[str, Any]]:
    import asyncio
    import concurrent.futures

    from fastmcp import Client

    from clio_agent.tools.gateway import gateway

    async def _list_tools() -> list[Any]:
        async with Client(gateway) as client:
            return await client.list_tools()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        tools = asyncio.run(_list_tools())
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            tools = pool.submit(lambda: asyncio.run(_list_tools())).result()

    capabilities = []
    for tool in sorted(tools, key=lambda item: item.name):
        description = tool.description or ""
        first_sentence = description.split(".")[0].strip() + "." if description else ""
        server = tool.name.split("_", 1)[0] if "_" in tool.name else tool.name
        capabilities.append(
            {
                "name": tool.name,
                "description": first_sentence,
                "server": server,
            }
        )
    return capabilities
