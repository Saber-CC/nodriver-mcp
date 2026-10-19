import base64
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mcp import ClientSession, StdioServerParameters
from mcp.client import Client
from mcp.client.stdio import stdio_client


class StdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_client_discovers_and_calls_official_binding_tools(self):
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "nodriver_mcp"]
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                self.assertEqual(initialized.server_info.name, "nodriver-mcp")
                self.assertTrue(initialized.server_info.version)
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
                    startup.input_schema["properties"]["persistent"]["default"]
                )
                failure = await session.call_tool("start_browser", {"timeout": -1})
                self.assertTrue(failure.is_error)
                self.assertIn(
                    "timeout must be nonnegative",
                    "".join(getattr(c, "text", "") for c in failure.content),
                )
                response = await session.call_tool(
                    "list_cdp_commands", {"domain": "WebMCP"}
                )
                self.assertFalse(response.is_error)
                self.assertIn(
                    "invoke_tool",
                    "".join(getattr(c, "text", "") for c in response.content),
                )

    async def test_modern_client_discovers_and_calls_tools(self):
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "nodriver_mcp"]
        )
        async with Client(parameters, mode="2026-07-28") as client:
            self.assertEqual(client.protocol_version, "2026-07-28")
            tools = await client.list_tools()
            self.assertIn("start_browser", {tool.name for tool in tools.tools})
            response = await client.call_tool(
                "list_cdp_commands", {"domain": "ServiceWorker"}
            )
            self.assertFalse(response.is_error, str(response))
            self.assertIn(
                "enable", "".join(getattr(c, "text", "") for c in response.content)
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
                    self.assertFalse(response.is_error, str(response))
                    return "".join(
                        getattr(item, "text", "") for item in response.content
                    )

                async with stdio_client(parameters) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        await call(
                            session, "start_browser", {"url": url, "persistent": True}
                        )
                        screenshot = await session.call_tool("take_screenshot", {})
                        self.assertFalse(screenshot.is_error, str(screenshot))
                        image = next(
                            item for item in screenshot.content if item.type == "image"
                        )
                        self.assertEqual(image.mime_type, "image/png")
                        self.assertTrue(
                            base64.b64decode(image.data, validate=True).startswith(
                                b"\x89PNG\r\n\x1a\n"
                            )
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
