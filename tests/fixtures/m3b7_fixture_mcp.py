"""A deliberately tiny MCP server for the M3B-7 real-CLI probe.

It exists so the probe can prove one thing end to end: that a real Reasonix turn can reach a real
MCP tool and that the resulting host lifecycle arrives through the adapter and the bridge as
persisted events. So it offers one pure function with no network, no clock, no randomness, no
database and no business meaning: whatever it is told, it echoes back.

Run standalone it speaks MCP over stdio, which is how the CLI starts it as a plugin.
"""
from mcp.server.fastmcp import FastMCP

mcp=FastMCP('m3b7_fixture')


@mcp.tool()
def echo_tool(value:str) -> dict:
    """Return the given value unchanged, with its length. Offline, deterministic, no side effects."""
    return {'echo':value,'length':len(value)}


if __name__=='__main__':mcp.run(transport='stdio')
