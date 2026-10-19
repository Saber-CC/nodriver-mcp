import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
                        "start_browser",
                        "close_browser",
                    }
                    <= names
                )
                startup = next(
                    tool for tool in tools.tools if tool.name == "start_browser"
                )
                self.assertFalse(
                    startup.inputSchema["properties"]["persistent"]["default"]
                )
                response = await session.call_tool(
                    "list_cdp_commands", {"domain": "WebMCP"}
                )
                self.assertFalse(response.isError)
                self.assertIn(
                    "invoke_tool",
                    "".join(getattr(c, "text", "") for c in response.content),
                )


@unittest.skipUnless(
    os.environ.get("NODRIVER_MCP_LIVE_TESTS") == "1", "Chrome integration is opt-in"
)
class StdioSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_profile_survives_mcp_process_restart(self):
        class Page(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = b"<title>Persistent MCP probe</title><p>local page</p>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        http = ThreadingHTTPServer(("127.0.0.1", 0), Page)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{http.server_port}/"
        try:
            with tempfile.TemporaryDirectory(
                prefix="nodriver-mcp-stdio-profile-"
            ) as directory:
                parameters = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "nodriver_mcp"],
                    env={
                        **os.environ,
                        "NODRIVER_HEADLESS": "true",
                        "NODRIVER_USER_DATA_DIR": directory,
                        "NODRIVER_PROXY": "",
                        "NODRIVER_EXTENSIONS": "[]",
                        "NODRIVER_BROWSER_ARGS": "[]",
                    },
                )

                async def call(session, name, arguments):
                    response = await session.call_tool(name, arguments)
                    self.assertFalse(response.isError, str(response))
                    return "".join(
                        getattr(item, "text", "") for item in response.content
                    )

                async with stdio_client(parameters) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        await call(
                            session, "start_browser", {"url": url, "persistent": True}
                        )
                        await call(
                            session,
                            "set_cookies",
                            {
                                "cookies": [
                                    {
                                        "name": "process_login",
                                        "value": "retained",
                                        "domain": "127.0.0.1",
                                        "path": "/",
                                        "expires": 1900000000.0,
                                    }
                                ]
                            },
                        )
                        await call(
                            session,
                            "set_local_storage",
                            {"items": {"process_storage": "retained"}},
                        )
                        # No explicit close: stdio lifespan shutdown must flush the profile.

                async with stdio_client(parameters) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        startup = json.loads(
                            await call(
                                session,
                                "start_browser",
                                {"url": url, "persistent": True},
                            )
                        )
                        self.assertTrue(startup["persistent"])
                        self.assertIn(
                            "process_login", await call(session, "get_cookies", {})
                        )
                        self.assertIn(
                            "process_storage: retained",
                            await call(session, "get_local_storage", {}),
                        )
                        await call(session, "close_browser", {})
                        await call(session, "new_page", {"url": url})
                        self.assertNotIn(
                            "process_login", await call(session, "get_cookies", {})
                        )
        finally:
            http.shutdown()
            http.server_close()
            thread.join()
