import sys
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class StdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_client_discovers_and_calls_official_binding_tools(self):
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "nodriver_mcp"]
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                self.assertTrue(
                    {
                        "take_screenshot",
                        "set_cookies",
                        "call_webmcp_tool",
                        "send_cdp_command",
                        "subscribe_cdp_events",
                    }
                    <= names
                )
                response = await session.call_tool(
                    "list_cdp_commands", {"domain": "WebMCP"}
                )
                self.assertFalse(response.isError)
                self.assertIn(
                    "invoke_tool",
                    "".join(getattr(c, "text", "") for c in response.content),
                )
