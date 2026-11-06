import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import anyio
import nodriver as uc
from mcp.server.mcpserver.exceptions import ToolError

from nodriver_mcp import launch
from nodriver_mcp import server as s
from nodriver_mcp.launch import LaunchOptions, ProfileLease


ENV = {
    "NODRIVER_PROXY": "",
    "NODRIVER_USER_DATA_DIR": "",
    "NODRIVER_BROWSER_PATH": "",
    "NODRIVER_BROWSER_ARGS": "[]",
    "NODRIVER_EXTENSIONS": "[]",
    "NODRIVER_HEADLESS": "true",
}


class LaunchOptionsTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_default_is_temporary_even_with_persistent_directory_configured(self):
        with patch.dict(os.environ, {"NODRIVER_USER_DATA_DIR": "/unused/persistent"}):
            options = LaunchOptions.resolve()
        self.assertFalse(options.persistent)
        self.assertIsNone(options.user_data_dir)

    def test_default_persistent_profile_reuses_the_legacy_directory(self):
        self.assertEqual(
            LaunchOptions.resolve(persistent=True).user_data_dir,
            str(Path.home() / ".nodriver-mcp" / "chrome-profile"),
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"NODRIVER_USER_DATA_DIR": directory}):
                self.assertEqual(
                    LaunchOptions.resolve(persistent=True).user_data_dir,
                    str(Path(directory).resolve()),
                )

    def test_named_profiles_are_distinct_and_cannot_escape_their_directory(self):
        a = LaunchOptions.resolve(persistent=True, profile="work")
        b = LaunchOptions.resolve(persistent=True, profile="personal")
        self.assertNotEqual(a.user_data_dir, b.user_data_dir)
        self.assertEqual(Path(a.user_data_dir).parent.name, "profiles")
        for profile in ("../other", "/tmp/other", ".", "", "x" * 65):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                LaunchOptions.resolve(persistent=True, profile=profile)
        with self.assertRaisesRegex(ValueError, "persistent=true"):
            LaunchOptions.resolve(profile="work")

    def test_explicit_proxy_and_direct_access_override_inherited_settings(self):
        with patch.dict(
            os.environ,
            {
                "NODRIVER_PROXY": "http://127.0.0.1:8000",
                "NODRIVER_BROWSER_ARGS": '["--proxy-server=http://old:80", "--no-proxy-server", "--disable-gpu"]',
            },
        ):
            options = LaunchOptions.resolve(proxy="http://127.0.0.1:7890")
            self.assertEqual(options.proxy, "http://127.0.0.1:7890")
            self.assertEqual(
                options.browser_args,
                ("--disable-gpu", "--proxy-server=http://127.0.0.1:7890"),
            )
            direct = LaunchOptions.resolve(proxy="")
            self.assertEqual(
                direct.browser_args, ("--disable-gpu", "--no-proxy-server")
            )
            self.assertEqual(LaunchOptions.resolve().proxy, "http://127.0.0.1:8000")

    def test_proxy_validation_accepts_native_schemes_and_rejects_invalid_addresses(
        self,
    ):
        for value, expected in (
            ("127.0.0.1:7890", "http://127.0.0.1:7890"),
            ("socks5://127.0.0.1:1080", "socks5://127.0.0.1:1080"),
            ("http://[::1]:7890/", "http://[::1]:7890"),
        ):
            with self.subTest(value=value):
                self.assertEqual(LaunchOptions.resolve(proxy=value).proxy, expected)
        for value in (
            "http://",
            "http://host:99999",
            "ftp://host:21",
            "http://host:80/path",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                LaunchOptions.resolve(proxy=value)
        with self.assertRaises(ValueError) as failure:
            LaunchOptions.resolve(proxy="http://user:secret@host:80")
        self.assertNotIn("secret", str(failure.exception))

    def test_profile_flags_and_malformed_configuration_are_rejected_before_launch(self):
        for variable, value in (
            ("NODRIVER_BROWSER_ARGS", "not-json"),
            ("NODRIVER_EXTENSIONS", "{}"),
            ("NODRIVER_BROWSER_ARGS", '["--user-data-dir=/another/profile"]'),
            ("NODRIVER_BROWSER_ARGS", '["--profile-directory", "Default"]'),
        ):
            with self.subTest(variable=variable, value=value):
                with (
                    patch.dict(os.environ, {variable: value}),
                    self.assertRaises(ValueError),
                ):
                    LaunchOptions.resolve()

    def test_profile_lease_prevents_concurrent_use_and_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = ProfileLease(directory), ProfileLease(directory)
            try:
                first.acquire()
                with self.assertRaisesRegex(ValueError, "already in use"):
                    second.acquire()
                first.release()
                second.acquire()
            finally:
                first.release()
                second.release()


class LaunchFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        s._browser_lock = asyncio.Lock()
        self.env = patch.dict(
            os.environ,
            {**ENV, "NODRIVER_BROWSER_PATH": "/nonexistent/nodriver-test-browser"},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    async def asyncTearDown(self):
        await s._shutdown_browser()

    async def test_failed_start_removes_native_temporary_profile_and_can_retry(self):
        configs = []
        config_class = uc.Config

        def capture_config(**kwargs):
            config = config_class(**kwargs)
            configs.append(config)
            return config

        with patch.object(s.uc, "Config", side_effect=capture_config):
            for _ in range(2):
                with self.assertRaises(FileNotFoundError):
                    await s.start_browser()
                self.assertIsNone(s._browser)
                self.assertIsNone(s._launch_options)
                self.assertIsNone(s._profile_lease)
                self.assertFalse(Path(configs[-1].user_data_dir).exists())
        self.assertNotEqual(configs[0].user_data_dir, configs[1].user_data_dir)

    async def test_failed_persistent_start_releases_lease_without_deleting_profile(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            with patch.dict(os.environ, {"NODRIVER_USER_DATA_DIR": directory}):
                with self.assertRaises(FileNotFoundError):
                    await s.start_browser(persistent=True)
                self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
                lease = ProfileLease(directory)
                try:
                    lease.acquire()
                finally:
                    lease.release()


class LaunchReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(
            patch.dict(os.environ, {**ENV, "NODRIVER_BROWSER_PATH": sys.executable})
        )
        self.enterContext(patch.object(s, "_browser_lock", asyncio.Lock()))
        self.enterContext(patch.object(uc.util, "free_port", return_value=9222))
        self.enterContext(patch.object(launch, "_DEVTOOLS_POLL_INTERVAL", 0))
        self.exited = asyncio.Event()
        self.process = Mock(pid=123, returncode=None)
        self.process.wait = AsyncMock(side_effect=self._wait_for_exit)
        self.process.terminate.side_effect = lambda: self._exit(-15)
        self.spawn = self.enterContext(
            patch.object(asyncio, "create_subprocess_exec", return_value=self.process)
        )
        self.http = self.enterContext(patch.object(launch, "HTTPConnection"))
        connection = self.http.return_value
        response = connection.getresponse.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = (
            b'{"webSocketDebuggerUrl":"ws://127.0.0.1:9222/devtools/browser/test"}'
        )
        self.attach = self.enterContext(patch.object(uc.Browser, "attach"))
        self.refresh = self.enterContext(patch.object(uc.Browser, "update_targets"))
        self.enterContext(patch.object(uc.Browser, "send", side_effect=self._command))
        self.prepare_page = self.enterContext(patch.object(s, "_prepare_page"))
        self.collect_network = self.enterContext(
            patch.object(s, "_auto_enable_network_collection")
        )
        self.created_pages = []
        self.page = self._tab("initial")

    async def asyncTearDown(self):
        await s._shutdown_browser()

    def _exit(self, code):
        self.process.returncode = code
        self.exited.set()

    async def _wait_for_exit(self):
        await self.exited.wait()
        return self.process.returncode

    async def _command(self, command):
        request = next(command)
        if request["method"] == "Browser.close":
            self._exit(0)
        elif request["method"] == "Target.createTarget":
            self.created_pages.append(request["params"]["url"])
            return self.page.target_id
        else:
            self.fail(f"Unexpected browser command: {request['method']}")

    @staticmethod
    def _tab(target_id):
        return SimpleNamespace(
            target_id=target_id,
            target=SimpleNamespace(
                type_="page", title="", url="about:blank", browser_context_id="default"
            ),
            parent=None,
            send=AsyncMock(),
            aclose=AsyncMock(),
            add_handler=Mock(),
            remove_handler=Mock(),
            back=AsyncMock(),
            forward=AsyncMock(),
            reload=AsyncMock(),
        )

    async def test_first_navigation_waits_for_debugger_and_initial_page(self):
        request = self.http.return_value.request
        request.side_effect = [ConnectionRefusedError(), ConnectionRefusedError(), None]
        url = "https://example.test/first"

        async def refresh():
            if self.refresh.await_count >= 2:
                s._browser._targets = [self.page]

        async def page_command(command):
            payload = next(command)
            if payload["method"] == "Page.navigate":
                self.prepare_page.assert_any_await(self.page)
                self.collect_network.assert_any_await(self.page)
                self.assertEqual(payload["params"]["url"], url)
                self.page.target.url = url
                return (None, None, None)

        async def run_action(_page, action, **_kwargs):
            return await action()

        self.refresh.side_effect = refresh
        self.page.send.side_effect = page_command
        with patch.object(s, "_perform_action", run_action):
            result, browser = await asyncio.gather(
                s.navigate_page(url=url), s._get_browser()
            )
        self.assertIn(f"Navigated to {url}", result)
        self.assertIs(browser, s._browser)
        self.assertEqual(s._selected_target_id, self.page.target_id)
        self.spawn.assert_awaited_once()
        self.attach.assert_awaited_once()
        self.assertEqual(request.call_count, 3)
        request.assert_called_with("GET", "/json/version")
        self.http.assert_called_with("127.0.0.1", 9222, timeout=0.5)
        self.assertEqual(self.created_pages, [])

    async def test_start_browser_waits_for_document_and_returns_the_refreshed_url(self):
        url = "https://example.test/first"
        final_url = "https://example.test/final"
        navigated = False

        async def refresh():
            s._browser._targets = [self.page]
            if navigated:
                self.assertEqual(evaluate.await_count, 3)
                self.page.target.url = final_url

        async def page_command(command):
            nonlocal navigated
            request = next(command)
            if request["method"] == "Page.navigate":
                self.assertEqual(request["params"]["url"], url)
                navigated = True
                return (None, None, None)
            self.assertEqual(request["method"], "Page.enable")

        async def probe(tab, expression, **_options):
            self.assertTrue(navigated)
            self.assertIs(tab, self.page)
            self.assertEqual(expression, "document.readyState")
            return next(states)

        states = iter(("loading", "interactive", "complete"))
        self.refresh.side_effect = refresh
        self.page.send.side_effect = page_command
        with (
            patch.object(s, "_evaluate", side_effect=probe) as evaluate,
            patch.object(s, "_PAGE_READY_POLL_INTERVAL", 0),
        ):
            result = await s.start_browser(url=url)
        self.assertEqual(result["ready_state"], "complete")
        self.assertEqual(result["url"], final_url)
        self.assertEqual(result["page_id"], self.page.target_id)

    async def test_start_browser_reports_incomplete_state_with_a_capped_wait(self):
        async def refresh():
            s._browser._targets = [self.page]

        self.refresh.side_effect = refresh
        self.page.send.return_value = (None, None, None)
        with patch.object(s, "_wait_for_page_ready", return_value="interactive") as wait:
            result = await s.start_browser(url="https://example.test/", timeout=0)
            wait.assert_awaited_once_with(self.page, timeout=10.0)
            self.assertEqual(result["ready_state"], "interactive")

            wait.reset_mock()
            result = await s.start_browser(url="https://example.test/", timeout=500)
            self.assertEqual(result["ready_state"], "interactive")
            self.assertEqual(wait.await_count, 1)
            self.assertGreater(wait.await_args.kwargs["timeout"], 0)
            self.assertLess(wait.await_args.kwargs["timeout"], 0.5)

    async def test_empty_inventory_creates_one_blank_page_and_waits_for_its_id(self):
        other = self._tab("unrelated")

        async def refresh():
            if self.created_pages:
                s._browser._targets = [other]
                if self.refresh.await_count >= 3:
                    s._browser._targets.append(self.page)

        self.refresh.side_effect = refresh
        with patch.object(s, "_INITIAL_PAGE_GRACE", 0):
            result = await s.start_browser()
        self.assertEqual(self.created_pages, ["about:blank"])
        self.assertEqual(result["page_id"], self.page.target_id)
        self.assertEqual(result["ready_state"], "unknown")
        self.prepare_page.assert_awaited_with(self.page)
        self.collect_network.assert_awaited_once_with(self.page)
        self.spawn.assert_awaited_once()

    async def test_browser_exit_interrupts_pending_devtools_connection(self):
        async def exit_during_attach():
            self._exit(17)
            await asyncio.Future()

        self.attach.side_effect = exit_during_attach
        with (
            self.assertLogs("nodriver-mcp", "WARNING"),
            self.assertRaisesRegex(ToolError, r"Chrome exited during startup .*17"),
        ):
            await s.mcp.call_tool("navigate_page", {"url": "https://example.test/"})
        self.refresh.assert_not_awaited()
        self.spawn.assert_awaited_once()
        self.assertIsNone(s._browser)

    async def test_devtools_timeout_retains_cause_and_cleans_up(self):
        self.http.return_value.request.side_effect = ConnectionRefusedError(
            "debugger not listening"
        )
        with (
            patch.object(launch, "_DEVTOOLS_STARTUP_TIMEOUT", 0.1),
            self.assertLogs("nodriver-mcp", "WARNING") as logs,
            self.assertRaisesRegex(ToolError, "waiting for the Chrome DevTools port"),
        ):
            await s.mcp.call_tool("navigate_page", {"url": "https://example.test/"})
        self.assertIn("ConnectionRefusedError: debugger not listening", logs.output[0])
        self.attach.assert_not_awaited()
        self.spawn.assert_awaited_once()
        self.process.terminate.assert_called_once()
        self.assertIsNone(s._browser)

    async def test_initial_page_timeout_also_bounds_target_discovery(self):
        async def stall_refresh():
            await asyncio.Future()

        self.refresh.side_effect = stall_refresh
        with (
            patch.object(s, "_INITIAL_PAGE_TIMEOUT", 0.05),
            self.assertLogs("nodriver-mcp", "WARNING"),
            self.assertRaisesRegex(ToolError, "waiting for Chrome's initial page"),
        ):
            await s.mcp.call_tool("navigate_page", {"url": "https://example.test/"})
        self.attach.assert_awaited_once()
        self.prepare_page.assert_not_awaited()
        self.assertEqual(self.process.returncode, 0)
        self.assertIsNone(s._browser)

    async def test_cancelled_startup_cleans_up_inside_mcp_cancel_scope(self):
        probing = asyncio.Event()

        async def stall_probe(_browser):
            probing.set()
            await asyncio.Future()

        with patch.object(launch, "_connect_devtools", side_effect=stall_probe):
            async with anyio.create_task_group() as group:
                group.start_soon(s._get_browser)
                await probing.wait()
                group.cancel_scope.cancel()
        self.process.terminate.assert_called_once()
        self.assertIsNone(s._browser)
        self.assertFalse(s._browser_lock.locked())
