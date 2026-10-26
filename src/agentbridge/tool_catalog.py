"""Discover the real MCP signatures without starting a runtime or network service."""
import asyncio
from types import SimpleNamespace

from .runtime import READ_TOOLS, WRITE_TOOLS
from .server import build_mcp


async def describe_async(access):
    if access not in {'read', 'full'}:
        raise ValueError('access must be read or full')
    allowed = READ_TOOLS if access == 'read' else READ_TOOLS | WRITE_TOOLS
    mcp = build_mcp(SimpleNamespace(allowed_tools=allowed))
    return [{'name': tool.name, 'description': tool.description or '', 'input_schema': tool.inputSchema}
            for tool in await mcp.list_tools()]


def describe(access):
    return asyncio.run(describe_async(access))
