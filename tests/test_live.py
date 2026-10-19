"""Opt-in integration tests: NODRIVER_MCP_LIVE_TESTS=1 uv run python -m unittest discover -s tests."""

import json
import io
import logging
import os
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from mcp.server.fastmcp import Image
from PIL import Image as PillowImage

from nodriver_mcp import server as s


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/api"):
            data, content_type = (
                b'{"ok":true,"source":"local-test"}',
                "application/json",
            )
        elif self.path.startswith("/frame"):
            data, content_type = (
                b'<input aria-label="Frame input"><button>Frame button</button>',
                "text/html",
            )
        else:
            html = """<title>Native integration</title>
            <input aria-label="Main input"><select aria-label="Choice"><option value="a">A</option><option value="b">B</option></select>
            <button onclick="window.clicked=true">Click probe</button>
            <iframe name="child" src="/frame"></iframe>
            <script>window.probe=42;</script>"""
            data, content_type = html.encode(), "text/html"
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@unittest.skipUnless(
    os.environ.get("NODRIVER_MCP_LIVE_TESTS") == "1", "Chrome integration is opt-in"
)
class LiveTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.http.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()

    async def asyncSetUp(self):
        logging.getLogger().setLevel(logging.ERROR)
        self.directory = tempfile.TemporaryDirectory(prefix="nodriver-mcp-integration-")
        self.env = patch.dict(
            os.environ,
            {
                "NODRIVER_HEADLESS": "true",
                "NODRIVER_USER_DATA_DIR": self.directory.name,
                "NODRIVER_EXTENSIONS": "[]",
                "NODRIVER_BROWSER_ARGS": '["--enable-blink-features=WebMCPSupport,WebMCPTesting"]'
                if "webmcp" in self._testMethodName
                else "[]",
            },
        )
        self.env.start()
        self.browser = await s._get_browser()
        result = await s.new_page(self.url)
        self.assertNotIn("Error", result)

    async def asyncTearDown(self):
        await s._shutdown_browser()
        self.env.stop()
        self.directory.cleanup()

    async def tool(self, tool_key, **kwargs):
        return await s.mcp._tool_manager.get_tool(tool_key).fn(**kwargs)

    async def test_default_temporary_session_restarts_clean_and_removes_profile(self):
        first = s._browser_launch_info()
        self.assertFalse(first["persistent"])
        self.assertNotEqual(first["user_data_dir"], self.directory.name)
        await s.set_cookie("temporary_login", "yes", "127.0.0.1")
        await s.set_local_storage({"temporary_storage": "yes"})
        reused = await s.start_browser()
        self.assertEqual(reused["status"], "reused")
        self.assertIn("temporary_login", await s.get_cookies())
        restarted = await s.start_browser(url=self.url, restart=True)
        self.assertEqual(restarted["status"], "restarted")
        self.assertFalse(Path(first["user_data_dir"]).exists())
        self.assertNotEqual(first["user_data_dir"], restarted["user_data_dir"])
        self.assertNotIn("temporary_login", await s.get_cookies())
        self.assertNotIn("temporary_storage", await s.get_local_storage())
        await s.close_browser()
        self.assertFalse(Path(restarted["user_data_dir"]).exists())

    async def test_persistent_start_restores_login_and_storage_after_close(self):
        temporary_path = Path(self.browser.config.user_data_dir)
        started = await s.start_browser(url=self.url, persistent=True, proxy="")
        self.assertEqual(started["status"], "restarted")
        self.assertFalse(temporary_path.exists())
        self.assertEqual(
            started["user_data_dir"], str(Path(self.directory.name).resolve())
        )
        await self.tool(
            "set_cookies",
            cookies=[
                {
                    "name": "persistent_login",
                    "value": "retained",
                    "domain": "127.0.0.1",
                    "path": "/",
                    "expires": 1900000000.0,
                }
            ],
        )
        await s.set_local_storage({"persistent_storage": "retained"})
        reused = await s.start_browser(url=self.url, persistent=True, proxy="")
        self.assertEqual(reused["status"], "reused")
        info = await self.tool("get_browser_info")
        self.assertTrue(info["launch"]["persistent"])
        await s.close_browser()
        self.assertTrue(Path(started["user_data_dir"]).exists())
        restored = await s.start_browser(url=self.url, persistent=True, proxy="")
        self.assertEqual(restored["user_data_dir"], started["user_data_dir"])
        self.assertIn("persistent_login", await s.get_cookies())
        self.assertIn("persistent_storage: retained", await s.get_local_storage())
        await s.close_browser()
        await s.new_page(self.url)
        self.assertFalse(s._browser_launch_info()["persistent"])
        self.assertNotIn("persistent_login", await s.get_cookies())

    async def test_startup_proxy_is_used_for_the_first_website_request(self):
        requests = []

        class Proxy(Handler):
            def do_GET(self):
                requests.append(self.path)
                super().do_GET()

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        address = f"http://127.0.0.1:{proxy.server_port}"
        url = "http://startup-proxy-probe.invalid/first-request"
        try:
            result = await s.start_browser(url=url, proxy=address, persistent=True)
            self.assertEqual(result["proxy"], address)
            self.assertTrue(result["persistent"])
            self.assertIn(url, requests)
            self.assertIn("Main input", await s.take_snapshot())
        finally:
            await s.close_browser()
            proxy.shutdown()
            proxy.server_close()
            thread.join()

    async def test_invalid_startup_options_keep_the_current_session(self):
        selected = s._selected_target_id
        with self.assertRaises(ValueError):
            await s.start_browser(proxy="not a proxy")
        self.assertIs(s._browser, self.browser)
        self.assertEqual(s._selected_target_id, selected)
        self.assertIn("Main input", await s.take_snapshot())

    async def test_native_element_cookie_screenshot_and_page_selection(self):
        snapshot = await s.take_snapshot()
        uid = re.search(r'uid=(\S+) textbox "Main input"', snapshot).group(1)
        self.assertNotIn("Error", await s.fill(uid, "hello 中文"))
        self.assertIn("hello 中文", await s.evaluate_script("el => el.value", [uid]))
        self.assertIsInstance(await s.take_screenshot(), Image)
        content = await s.mcp.call_tool("take_screenshot", {})
        if isinstance(content, tuple):
            content = content[0]
        self.assertTrue(any(item.type == "image" for item in content))
        await s.evaluate_script(
            "() => {document.body.style.minHeight='2400px'; return true;}"
        )
        full = await s.take_screenshot(full_page=True)
        self.assertGreaterEqual(PillowImage.open(io.BytesIO(full.data)).height, 2400)
        first_target = s._selected_target_id
        await s.new_page("about:blank", background=True)
        self.assertEqual(s._selected_target_id, first_target)
        self.assertEqual((await s._active_tab()).target_id, first_target)
        await self.tool(
            "set_cookies",
            cookies=[
                {
                    "name": "native",
                    "value": "yes",
                    "domain": "127.0.0.1",
                    "path": "/",
                    "expires": 1900000000.0,
                }
            ],
        )
        self.assertIn("native", await s.get_cookies())
        path = str(Path(self.directory.name) / "cookies.dat")
        await self.tool("save_cookies", file_path=path)
        await self.tool("clear_cookies")
        self.assertNotIn('"native"', await s.get_cookies())
        await self.tool("load_cookies", file_path=path)
        self.assertIn('"native"', await s.get_cookies())
        await s.set_local_storage({"session_probe": "restored"})
        session_dir = Path(self.directory.name) / "sessions"
        with patch.object(s, "_SESSIONS_DIR", str(session_dir)):
            await s.save_session("native-json")
            session_file = next(session_dir.glob("*.json"))
            await self.tool("clear_cookies")
            await s.evaluate_script("() => localStorage.clear()")
            restored = await s.load_session(str(session_file))
            self.assertIn("Cookies restored: 1", restored)
            self.assertIn('"native"', await s.get_cookies())
            self.assertIn("restored", await s.get_local_storage())

    async def test_isolated_context_and_same_process_frames(self):
        frames = await self.tool("list_frames")
        child = next(frame for frame in frames if frame["name"] == "child")
        await self.tool("select_frame", frame_id=child["frame_id"])
        self.assertIn("Frame input", await s.take_snapshot())
        scoped = await self.tool("find_elements", selector="input")
        self.assertEqual(len(scoped), 1)
        self.assertNotIn("Error", await s.fill(scoped[0]["uid"], "frame-scoped"))
        self.assertIn(
            "frame-scoped",
            await s.evaluate_script("() => document.querySelector('input').value"),
        )
        self.assertIn(
            "INPUT",
            await s.evaluate_script("() => document.querySelector('input').tagName"),
        )
        await self.tool("select_frame")
        self.assertIn("Frame input", await s.take_snapshot(include_frames=True))
        await s.set_cookie("main_only", "yes", "127.0.0.1")
        result = await s.new_page(self.url, isolated_context="isolated")
        self.assertNotIn("Error", result)
        self.assertNotIn("main_only", await s.get_cookies())
        await s.set_cookie("isolated_only", "yes", "127.0.0.1")
        self.assertIn("isolated_only", await s.get_cookies())

    async def test_network_console_and_trace(self):
        await s.evaluate_script("() => fetch('/api').then(r => r.json())")
        listing = await s.list_network_requests(
            resource_types=["fetch"], url_filter="/api"
        )
        request_id = int(re.search(r"\[(\d+)\]", listing).group(1))
        self.assertIn("local-test", await s.get_network_request(request_id))
        await s.enable_console_collection()
        await s.evaluate_script("() => console.log('native-probe', 0, false)")
        self.assertIn("native-probe 0 False", await s.list_console_messages())
        await s.disable_console_collection()
        tab = await s._active_tab()
        self.assertFalse(tab.handlers.get(s.uc.cdp.runtime.ConsoleAPICalled))
        await s.performance_start_trace(reload=False, auto_stop=False)
        await s.evaluate_script("() => { performance.mark('probe'); return 1; }")
        path = str(Path(self.directory.name) / "trace.json")
        await s.performance_stop_trace(path)
        self.assertTrue(json.loads(Path(path).read_text())["traceEvents"])

    async def test_webmcp_discovery_and_invocation(self):
        result = await s.evaluate_script("""() => {
            if (!navigator.modelContext) return 'unsupported';
            navigator.modelContext.registerTool({
                name: 'echo_probe', description: 'Echo a local integration test value',
                inputSchema: {type:'object', properties:{message:{type:'string'}}, required:['message']},
                execute: async ({message}) => ({content:[{type:'text', text:message}]})
            });
            return 'registered';
        }""")
        if "unsupported" in result:
            self.skipTest("Chrome does not expose navigator.modelContext")
        self.assertNotIn("Error", result)
        tools = await self.tool("list_webmcp_tools")
        self.assertTrue(any(tool["name"] == "echo_probe" for tool in tools))
        response = await self.tool(
            "call_webmcp_tool",
            tool_name="echo_probe",
            input={"message": "native-webmcp-success"},
            timeout=5000,
        )
        self.assertIn("native-webmcp-success", json.dumps(response))

    async def test_native_context_search_and_generic_cdp(self):
        script = await s.evaluate_script("() => ({probe:42,flag:false})")
        self.assertEqual(
            json.loads(script.split("\n")[1]), {"probe": 42, "flag": False}
        )
        found = await self.tool("find_elements", selector="input")
        self.assertTrue(found)
        best = await self.tool("find_elements", text="Click probe", best_match=True)
        self.assertTrue(best)
        self.assertNotIn("Error", await s.fill(found[0]["uid"], "native-found"))
        xpath = await self.tool("find_elements", xpath="//button", timeout=0)
        self.assertTrue(xpath)
        version = await self.tool(
            "send_cdp_command", domain="Browser", command="getVersion", scope="browser"
        )
        self.assertIn("Chrome", version[1])
        await self.tool("configure_network_capture")
        result = await self.tool("create_browser_context", name="native-created")
        self.assertIn("Created", result)
        self.assertIsNotNone((await s._page_tab()).target.browser_context_id)
