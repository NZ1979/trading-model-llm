"""On-demand market data MCP server.

Named `mcp_server`, deliberately NOT `mcp`. A local package named `mcp`
shadows the installed MCP SDK completely, at which point
`from mcp.server import MCPServer` fails from inside the server itself.
`docs/FEED_SPEC_V4.md` §7 specifies `mcp/server.py` and is wrong on this
point. `tests/test_mcp_server.py` asserts the collision cannot be
reintroduced.
"""
