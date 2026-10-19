import asyncio
import base64
import collections
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nodriver as uc
from mcp.server.mcpserver import Image
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from pydantic import ValidationError

from nodriver_mcp import server as s


class FakeTab:
    def __init__(self, name):
        self.target = SimpleNamespace(
            target_id=name,
            url=f"https://{name}.test",
            title=name,
            browser_context_id=None,
        )
        self.target_id = name
        self.parent = None
        self.handlers = collections.defaultdict(list)
        self.activate = AsyncMock()
        self.send = AsyncMock(return_value=None)

    def add_handler(self, event, callback):
        if callback not in self.handlers[event]:
            self.handlers[event].append(callback)

    def remove_handler(self, event, callback):
        self.handlers[event].remove(callback)


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.a, self.b = FakeTab("a"), FakeTab("b")
        self.browser = SimpleNamespace(
            tabs=[self.a, self.b], update_targets=AsyncMock()
        )
        self.patch = patch.object(
            s, "_get_browser", AsyncMock(return_value=self.browser)
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.prepare_page = s._prepare_page
        prepare_patch = patch.object(s, "_prepare_page", AsyncMock())
        prepare_patch.start()
        self.addCleanup(prepare_patch.stop)

        async def perform(_tab, action, **_options):
            return await action()

        action_patch = patch.object(s, "_perform_action", perform)
        action_patch.start()
        self.addCleanup(action_patch.stop)
        self.addAsyncCleanup(s._shutdown_callbacks[0])
        s._selected_target_id = "a"
        for value in (
            s._selected_frames,
            s._focused_pages,
            s._closing_page_ids,
            s._frame_connections,
            s._frame_loaders,
            s._frame_discovery_locks,
            s._uid_owners,
            s._unique_id_to_mcp_id,
            s._uid_to_backend_node_id,
            s._console_callbacks,
            s._network_callbacks,
            s._network_collection_enabled_tabs,
            s._console_collection_enabled_tabs,
            s._network_requests,
            s._console_messages,
            s._preserved_network_requests,
            s._preserved_console_messages,
        ):
            value.clear()
        s._tracing_active = False
        s._trace_stopping = False
        s._trace_tab = None

    async def test_tool_timeouts_keep_details_and_unexpected_failures_stay_generic(
        self,
    ):
        with patch.object(
            s, "_active_tab", AsyncMock(side_effect=TimeoutError("Capture timed out"))
        ):
            with self.assertRaisesRegex(ToolError, "Capture timed out"):
                await s.mcp.call_tool("take_screenshot", {})
        with patch.object(
            s, "_active_tab", AsyncMock(side_effect=RuntimeError("internal diagnostic"))
        ):
            with self.assertRaises(UnexpectedToolError) as raised:
                await s.mcp.call_tool("take_screenshot", {})
            self.assertNotIn("internal diagnostic", str(raised.exception))

    async def test_invalid_tool_output_keeps_sdk_validation_failure(self):
        async def invalid_output() -> int:
            return {"unexpected": "output"}

        s.mcp.add_tool(invalid_output, name="invalid_output_probe")
        self.addCleanup(s.mcp.remove_tool, "invalid_output_probe")
        with self.assertRaises(UnexpectedToolError) as raised:
            await s.mcp.call_tool("invalid_output_probe", {})
        self.assertIsInstance(raised.exception.__cause__, ValidationError)

    async def test_chrome_protocol_failures_keep_their_code_and_reason(self):
        for code, message in (
            (-32000, "No node with given id found"),
            (-32601, "Experimental command was not found"),
        ):
            with self.subTest(code=code):
                self.a.send.side_effect = uc.ProtocolException(
                    {"code": code, "message": message}
                )
                with self.assertRaises(ToolError) as raised:
                    await s.mcp.call_tool(
                        "send_cdp_command",
                        {
                            "domain": "DOM",
                            "command": "describeNode",
                            "parameters": {"backendNodeId": 404},
                        },
                    )
                self.assertIn(message, str(raised.exception))
                self.assertIn(str(code), str(raised.exception))

    async def test_selected_page_persists_for_subsequent_tools(self):
        await s.select_page(0)
        self.assertIs(await s._active_tab(), self.a)
        await s.select_page(1, bring_to_front=False)
        self.assertIs(await s._active_tab(), self.b)
        self.b.activate.assert_not_awaited()

    async def test_page_focus_preparation_retries_failures_and_reuses_connections(self):
        error = uc.ProtocolException({"code": -32000, "message": "Session detached"})
        self.a.send.side_effect = [error, None]
        with self.assertRaises(uc.ProtocolException):
            await self.prepare_page(self.a)
        self.assertNotIn(id(self.a), s._focused_pages)
        await self.prepare_page(self.a)
        await self.prepare_page(self.a)
        self.assertEqual(self.a.send.await_count, 2)
        self.assertEqual(
            next(self.a.send.call_args.args[0]),
            {
                "method": "Emulation.setFocusEmulationEnabled",
                "params": {"enabled": True},
            },
        )
        s._clear_collection_state(self.a)
        self.assertNotIn(id(self.a), s._focused_pages)

    async def test_page_ids_are_exposed_in_tool_schemas(self):
        for name in (
            "take_snapshot",
            "click",
            "fill_form",
            "press_key",
            "take_screenshot",
            "navigate_page",
            "emulate",
            "list_frames",
            "find_elements",
            "set_cookies",
            "send_cdp_command",
            "list_console_messages",
            "get_network_request",
            "start_screen_recording",
        ):
            with self.subTest(tool=name):
                schema = s.mcp._tool_manager.get_tool(name).parameters
                self.assertIn("page_id", schema["properties"])
                self.assertNotIn("page_id", schema.get("required", []))
                self.assertEqual(schema["properties"]["page_id"]["default"], None)

    async def test_explicit_page_calls_overlap_without_changing_default_selection(self):
        entered, release = asyncio.Event(), asyncio.Event()
        seen = set()

        def sender(name):
            async def send(command):
                message = next(command)
                if message["method"] != "Runtime.evaluate":
                    return None
                seen.add(name)
                if len(seen) == 2:
                    entered.set()
                await release.wait()
                return uc.cdp.runtime.RemoteObject(type_="string", value=name), None

            return send

        self.a.send.side_effect = sender("a")
        self.b.send.side_effect = sender("b")
        tasks = [
            asyncio.create_task(s.evaluate_script("location.href", page_id=page))
            for page in ("a", "b")
        ]
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual(s._selected_target_id, "a")
            await s.select_page("b", bring_to_front=False)
        finally:
            release.set()
        results = await asyncio.gather(*tasks)
        self.assertIn('"a"', results[0])
        self.assertIn('"b"', results[1])
        self.assertIsNone(s._page_call.get())
        self.assertIs(await s._page_tab(), self.b)
        self.a.activate.assert_not_awaited()
        self.b.activate.assert_not_awaited()

    async def test_default_page_is_pinned_through_nested_snapshot_work(self):
        s._uid_owners["button"] = ("a", self.a, "")
        observed = []

        async def click(*_args):
            s._selected_target_id = "b"
            await asyncio.sleep(0)

        async def snapshot():
            observed.append(await s._page_tab())
            return "pinned snapshot"

        with (
            patch.object(s, "_get_box_model", AsyncMock(return_value=(10, 20))),
            patch.object(s, "_dispatch_mouse_click", click),
            patch.object(s, "take_snapshot", snapshot),
        ):
            result = await s.click("button", include_snapshot=True)
        self.assertIn("pinned snapshot", result)
        self.assertEqual(observed, [self.a])
        self.assertIs(await s._page_tab(), self.b)

    async def test_stable_ids_survive_reordering_and_closed_ids_never_fall_back(self):
        self.a.get_local_storage = AsyncMock(return_value={"owner": "page-a"})
        self.b.get_local_storage = AsyncMock(return_value={"owner": "page-b"})
        self.browser.tabs.reverse()
        self.assertIn("page-a", await s.get_local_storage(page_id="a"))
        self.assertIn("page-b", await s.get_local_storage(page_id=0))
        self.assertEqual(s._selected_target_id, "a")
        self.browser.tabs = [self.b]
        for page_id in ("a", "missing", -1, 3):
            with (
                self.subTest(page_id=page_id),
                self.assertRaisesRegex(ValueError, "page_id"),
            ):
                await s.get_local_storage(page_id=page_id)
        self.assertEqual(self.b.get_local_storage.await_count, 1)
        self.assertIsNone(s._page_call.get())

    async def test_cancelled_explicit_call_restores_default_page_context(self):
        async def send(command):
            if next(command)["params"]["type"] != "keyUp":
                raise asyncio.CancelledError()

        self.b.send.side_effect = send
        with self.assertRaises(asyncio.CancelledError):
            await s.press_key("Control+A", page_id="b")
        self.assertIsNone(s._page_call.get())
        self.assertIs(await s._page_tab(), self.a)
        s._uid_owners["foreign"] = ("b", self.b, "")
        self.assertIn("stale", await s.click("foreign", page_id="a"))
        self.a.send.assert_not_awaited()

    async def test_preserved_navigation_history_is_retained_per_page(self):
        for tab in (self.a, self.b, self.b, self.b, self.b):
            marker = f"{tab.target_id}-{len(s._preserved_network_requests)}"
            s._network_requests.append(
                {
                    "page_id": tab.target_id,
                    "reqid": marker,
                    "method": "GET",
                    "type": "XHR",
                    "url": marker,
                }
            )
            s._preserve_on_navigation(tab)
        a = await s.list_network_requests(include_preserved_requests=True, page_id="a")
        b = await s.list_network_requests(include_preserved_requests=True, page_id="b")
        self.assertIn("a-0", a)
        self.assertNotIn("b-", a)
        self.assertIn("3 of 3", b)
        self.assertNotIn("a-", b)

    async def test_event_subscriptions_validate_page_and_close_cleans_only_its_owner(
        self,
    ):
        tool = lambda name: s.mcp._tool_manager.get_tool(name).fn
        first = await tool("subscribe_cdp_events")(
            ["Runtime.consoleAPICalled"], enable_domain=False, page_id="a"
        )
        second = await tool("subscribe_cdp_events")(
            ["Runtime.consoleAPICalled"], enable_domain=False, page_id="b"
        )
        for name in ("get_cdp_events", "unsubscribe_cdp_events"):
            with self.assertRaisesRegex(ValueError, "different page_id"):
                await tool(name)(first, page_id="b")
        self.assertEqual(len(self.a.handlers[uc.cdp.runtime.ConsoleAPICalled]), 1)
        await s._forget_page_state(self.a)
        self.assertFalse(self.a.handlers[uc.cdp.runtime.ConsoleAPICalled])
        self.assertEqual(len(self.b.handlers[uc.cdp.runtime.ConsoleAPICalled]), 1)
        self.assertEqual(await tool("get_cdp_events")(second, page_id="b"), [])
        with self.assertRaisesRegex(ValueError, "Unknown subscription"):
            await tool("get_cdp_events")(first)
        with self.assertRaisesRegex(ValueError, "scope='page'"):
            await tool("send_cdp_command")(
                "Browser", "getVersion", scope="browser", page_id="a"
            )

    async def test_recordings_belong_to_each_page_and_default_stop_keeps_origin(self):
        tool = lambda name: s.mcp._tool_manager.get_tool(name).fn
        self.a.send.return_value = "stream-a"
        self.b.send.return_value = "stream-b"
        with tempfile.TemporaryDirectory() as directory:
            a, b = Path(directory) / "a.webm", Path(directory) / "b.webm"
            await asyncio.gather(
                tool("start_screen_recording")(str(a), page_id="a"),
                tool("start_screen_recording")(str(b), page_id="b"),
            )
            with self.assertRaisesRegex(ValueError, "already active"):
                await tool("start_screen_recording")(str(a), page_id="a")

            async def stream(tab, _handle):
                return str(tab.target_id).encode()

            with patch.object(s, "_read_stream", stream):
                await tool("stop_screen_recording")(page_id="b")
                self.assertFalse(a.exists())
                self.assertEqual(b.read_bytes(), b"b")
                s._selected_target_id = "b"
                await tool("stop_screen_recording")()
                self.assertEqual(a.read_bytes(), b"a")

    async def test_trace_reserves_browser_wide_owner_before_first_send(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(command):
            if next(command)["method"] == "Tracing.start":
                entered.set()
                await release.wait()

        self.a.send.side_effect = send
        first = asyncio.create_task(
            s.performance_start_trace(reload=False, auto_stop=False, page_id="a")
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertIn(
                "already running",
                await s.performance_start_trace(
                    reload=False, auto_stop=False, page_id="b"
                ),
            )
            with self.assertRaisesRegex(ValueError, "different page_id"):
                await s.performance_stop_trace(page_id="b")
        finally:
            release.set()
        self.assertIn("Trace started", await first)
        self.assertIs(s._trace_tab, self.a)
        self.b.send.assert_not_awaited()

    async def test_concurrent_closes_keep_one_page_open(self):
        closed = []

        def closer(tab):
            async def close():
                await asyncio.sleep(0)
                closed.append(tab)
                self.browser.tabs.remove(tab)

            return close

        for tab in (self.a, self.b):
            tab.close = closer(tab)
            tab.aclose = AsyncMock()
        results = await asyncio.gather(s.close_page("a"), s.close_page("b"))
        self.assertEqual(len(closed), 1)
        self.assertEqual(len(self.browser.tabs), 1)
        self.assertTrue(any("last open page" in result for result in results))
        self.assertFalse(s._closing_page_ids)

    async def test_shortcut_events_include_modifiers_and_physical_key_codes(self):
        events = []

        async def send(command):
            events.append(next(command)["params"])

        self.a.send.side_effect = send
        await s.press_key("Control+Shift+a")
        target = next(
            event
            for event in events
            if event["key"].lower() == "a" and event["type"] != "keyUp"
        )
        self.assertEqual(target["key"], "A")
        self.assertEqual(target["code"], "KeyA")
        self.assertEqual(target["windowsVirtualKeyCode"], 65)
        self.assertEqual(target["modifiers"], 10)
        self.assertFalse(target.get("text"))
        self.assertEqual(
            [(event["key"], event["modifiers"]) for event in events[-2:]],
            [("Shift", 2), ("Control", 0)],
        )

    async def test_plus_shortcuts_keep_the_literal_target_key(self):
        for shortcut, mask in (("+", 0), ("Control++", 2), ("Control+Shift++", 10)):
            with self.subTest(shortcut=shortcut):
                events = []

                async def send(command):
                    events.append(next(command)["params"])

                self.a.send.side_effect = send
                await s.press_key(shortcut)
                target = next(
                    event
                    for event in events
                    if event["key"] == "+" and event["type"] != "keyUp"
                )
                self.assertEqual(target["code"], "Equal")
                self.assertEqual(target["modifiers"], mask)
                self.assertTrue(all(event["key"] for event in events))

    async def test_invalid_shortcuts_are_rejected_before_opening_a_page(self):
        with patch.object(s, "_active_tab", AsyncMock()) as active:
            for shortcut in (
                "",
                "Control+",
                "Control+++",
                "Control+Control+A",
                "Bogus+A",
                "NoSuchKey",
            ):
                with self.subTest(shortcut=shortcut), self.assertRaises(ValueError):
                    await s.press_key(shortcut)
            active.assert_not_awaited()

    async def test_key_failures_release_remaining_keys_and_keep_original_error(self):
        for failure_type in ("down", "up", "cancel"):
            with self.subTest(failure_type=failure_type):
                events = []
                error = (
                    asyncio.CancelledError()
                    if failure_type == "cancel"
                    else uc.ProtocolException(
                        {"code": -32000, "message": "Key dispatch failed"}
                    )
                )

                async def send(command):
                    event = next(command)["params"]
                    events.append(event)
                    if event["key"] == "A" and (
                        event["type"] == "keyUp"
                        if failure_type == "up"
                        else event["type"] != "keyUp"
                    ):
                        raise error
                    if event["key"] == "Shift" and event["type"] == "keyUp":
                        raise RuntimeError("Secondary release failure")

                self.a.send.side_effect = send
                with self.assertRaises(type(error)) as raised:
                    await s.press_key("Control+Shift+A")
                self.assertIs(raised.exception, error)
                releases = [event for event in events if event["type"] == "keyUp"]
                self.assertEqual(
                    [event["key"] for event in releases], ["A", "Shift", "Control"]
                )
                self.assertEqual(releases[-1]["modifiers"], 0)

    async def test_disable_console_removes_exact_callback_and_does_not_accumulate(self):
        for _ in range(2):
            await s._enable_console_collection(self.a)
            self.assertEqual(len(self.a.handlers[uc.cdp.runtime.ConsoleAPICalled]), 1)
            await s._disable_console_collection(self.a)
            self.assertEqual(self.a.handlers[uc.cdp.runtime.ConsoleAPICalled], [])

    async def test_failed_network_enable_can_be_retried(self):
        self.a.send.side_effect = RuntimeError("disconnected")
        with self.assertRaises(RuntimeError):
            await s._auto_enable_network_collection(self.a)
        self.assertNotIn(s._tab_key(self.a), s._network_collection_enabled_tabs)
        self.assertFalse(any(self.a.handlers.values()))
        self.a.send.side_effect = None
        await s._auto_enable_network_collection(self.a)
        self.assertIn(s._tab_key(self.a), s._network_collection_enabled_tabs)

    async def test_collectors_are_isolated_for_connections_with_the_same_target_id(
        self,
    ):
        replacement = FakeTab(self.a.target_id)
        for tab in (self.a, replacement):
            self.assertTrue(await s._enable_console_collection(tab))
            await s._auto_enable_network_collection(tab)
            self.assertEqual(len(tab.handlers[uc.cdp.runtime.ConsoleAPICalled]), 1)
            self.assertEqual(len(tab.handlers[uc.cdp.network.RequestWillBeSent]), 1)
        await s._disable_console_collection(self.a)
        s._clear_collection_state(self.a)
        self.assertFalse(self.a.handlers[uc.cdp.runtime.ConsoleAPICalled])
        self.assertEqual(len(replacement.handlers[uc.cdp.runtime.ConsoleAPICalled]), 1)
        self.assertEqual(len(replacement.handlers[uc.cdp.network.RequestWillBeSent]), 1)
        self.assertFalse(await s._enable_console_collection(replacement))
        await s._auto_enable_network_collection(replacement)
        self.assertEqual(len(replacement.handlers[uc.cdp.network.RequestWillBeSent]), 1)

    async def test_retiring_a_frame_removes_only_its_collection_state(self):
        frame = FakeTab("frame")
        frame.parent = self.a
        frame.aclose = AsyncMock()
        frame_key = (self.a.target_id, frame.target_id)
        s._frame_connections[frame_key] = frame
        s._frame_loaders[frame_key] = "old-loader"
        self.a.get_frame_tree = AsyncMock(
            return_value=SimpleNamespace(
                frame=SimpleNamespace(id_=self.a.target_id, loader_id="root-loader"),
                child_frames=None,
            )
        )
        self.a.get_frames = AsyncMock(return_value=[])
        for tab in (self.a, frame):
            await s._enable_console_collection(tab)
            await s._auto_enable_network_collection(tab)
        external_console = lambda event: None
        external_network = lambda event: None
        frame.add_handler(uc.cdp.runtime.ConsoleAPICalled, external_console)
        frame.add_handler(uc.cdp.network.RequestWillBeSent, external_network)

        await s._discover_frames(self.a)
        frame.aclose.assert_awaited_once()
        self.assertNotIn(s._tab_key(frame), s._console_collection_enabled_tabs)
        self.assertNotIn(s._tab_key(frame), s._network_collection_enabled_tabs)
        self.assertNotIn(s._tab_key(frame), s._console_callbacks)
        self.assertNotIn(s._tab_key(frame), s._network_callbacks)
        self.assertEqual(
            frame.handlers[uc.cdp.runtime.ConsoleAPICalled], [external_console]
        )
        self.assertEqual(
            frame.handlers[uc.cdp.network.RequestWillBeSent], [external_network]
        )
        self.assertFalse(await s._enable_console_collection(self.a))
        self.assertEqual(len(self.a.handlers[uc.cdp.runtime.ConsoleAPICalled]), 1)
        self.assertEqual(len(self.a.handlers[uc.cdp.network.RequestWillBeSent]), 1)

    async def test_request_ids_survive_filters_and_do_not_cross_pages(self):
        for rid, tab, resource in [
            (51, self.a, "Document"),
            (72, self.a, "XHR"),
            (90, self.b, "XHR"),
        ]:
            s._network_requests.append(
                {
                    "reqid": rid,
                    "id": str(rid),
                    "page_id": tab.target_id,
                    "connection": tab,
                    "type": resource,
                    "url": tab.target.url + f"/{rid}",
                    "method": "GET",
                    "post_data": "",
                }
            )
        listing = await s.list_network_requests(resource_types=["xhr"])
        self.assertIn("[72]", listing)
        self.assertNotIn("[90]", listing)
        self.a.send.return_value = ("response", False)
        result = await s.get_network_request(72)
        self.assertIn("/72", result)
        self.b.send.assert_not_awaited()

    async def test_wrong_page_uid_is_rejected(self):
        s._uid_owners["old"] = ("b", self.b, "")
        with self.assertRaisesRegex(ValueError, "stale"):
            await s._tab_for_uid("old")

    async def test_fill_form_preserves_failure(self):
        with patch.object(
            s, "fill", AsyncMock(return_value="Error filling uid=missing: stale")
        ):
            result = await s.fill_form([{"uid": "missing", "value": "x"}])
        self.assertIn("Error", result)
        self.assertNotIn(": filled", result)

    async def test_element_script_unpacks_result_and_reports_js_errors(self):
        s._uid_owners["el"] = ("a", self.a, "")
        with patch.object(
            s, "_resolve_uid", AsyncMock(return_value=SimpleNamespace(object_id="obj"))
        ):
            self.a.send.return_value = (SimpleNamespace(value=0), None)
            self.assertIn(
                "\\n0\\n".replace("\\n", "\n"),
                await s.evaluate_script("el => 0", ["el"]),
            )

    async def test_native_evaluation_preserves_false_and_normal_json_objects(self):
        self.a.send.return_value = (
            uc.cdp.runtime.RemoteObject(type_="boolean", value=False),
            None,
        )
        self.assertIs(await s._evaluate(self.a, "false"), False)
        self.a.send.return_value = (
            uc.cdp.runtime.RemoteObject(type_="object", value={"ok": True}),
            None,
        )
        self.assertEqual(await s._evaluate(self.a, "({ok:true})"), {"ok": True})

    async def test_native_screenshot_returns_mcp_image_without_writing_file(self):
        self.a.save_screenshot = AsyncMock(
            return_value=base64.b64encode(b"png-bytes").decode()
        )
        result = await s.take_screenshot()
        self.assertIsInstance(result, Image)
        self.a.save_screenshot.assert_awaited_once_with(
            format="png", full_page=False, as_base64=True
        )

    async def test_official_cookiejar_uses_selected_context(self):
        from nodriver.core.browser import CookieJar

        jar = await s._cookie_jar()
        self.assertIsInstance(jar, CookieJar)
        self.a.send.return_value = []
        await jar.get_all()
        generator = self.a.send.call_args.args[0]
        self.assertEqual(next(generator)["method"], "Storage.getCookies")
        self.b.send.assert_not_awaited()

    async def test_session_cookie_expiry_uses_official_type(self):
        cookie = s._cookie_param(
            {
                "name": "test",
                "value": "x",
                "expires": 1900000000.0,
                "sameSite": "Lax",
                "httpOnly": True,
            }
        )
        self.assertIsInstance(cookie.expires, uc.cdp.network.TimeSinceEpoch)
        self.assertEqual(cookie.to_json()["expires"], 1900000000.0)
        self.assertNotIn(
            "expires",
            s._cookie_param({"name": "s", "value": "v", "expires": -1}).to_json(),
        )

    async def test_isolated_native_cookiejar_passes_context_to_official_storage(self):
        self.a.target.browser_context_id = uc.cdp.browser.BrowserContextID("isolated")
        self.browser.send = AsyncMock(return_value=[])
        jar = await s._cookie_jar()
        await jar.get_all()
        command = self.browser.send.call_args.args[0]
        self.assertEqual(
            next(command),
            {
                "method": "Storage.getCookies",
                "params": {"browserContextId": "isolated"},
            },
        )
        self.a.send.assert_not_awaited()

    async def test_navigation_keeps_existing_session(self):
        self.a.send.return_value = ("frame", "loader", None, None)
        await s._navigate_url(self.a, "https://example.test")
        command = self.a.send.call_args.args[0]
        self.assertEqual(next(command)["method"], "Page.navigate")
        self.assertFalse(hasattr(self.a, "attach"))

    async def test_webmcp_result_arriving_before_waiter_is_preserved(self):
        def dispatch(event):
            for callback in self.a.handlers[type(event)]:
                callback(event)

        async def send(command):
            message = next(command)
            if message["method"] == "WebMCP.enable":
                dispatch(
                    uc.cdp.web_mcp.ToolsAdded(
                        [
                            uc.cdp.web_mcp.Tool.from_json(
                                {
                                    "name": "echo",
                                    "description": "Echo",
                                    "frameId": "frame",
                                }
                            )
                        ]
                    )
                )
            if message["method"] == "WebMCP.invokeTool":
                dispatch(
                    uc.cdp.web_mcp.ToolResponded.from_json(
                        {
                            "invocationId": "completed",
                            "status": "Completed",
                            "output": {"ok": True},
                        }
                    )
                )
                return "completed"

        self.a.send.side_effect = send
        invoke = s.mcp._tool_manager.get_tool("call_webmcp_tool").fn
        result = await invoke("echo", {"message": "probe"})
        self.assertEqual(result["output"], {"ok": True})
        await s._shutdown_callbacks[0]()

    async def test_trace_consumes_stream_and_keeps_originating_connection(self):
        trace = json.dumps({"traceEvents": [{"name": "probe"}]}).encode()

        async def send(generator):
            message = next(generator)
            if message["method"] == "Tracing.end":
                event = SimpleNamespace(stream="stream")
                for callback in self.a.handlers[uc.cdp.tracing.TracingComplete]:
                    callback(event)
            return None

        self.a.send.side_effect = send
        s._tracing_active, s._trace_tab, s._selected_target_id = True, self.a, "b"
        with patch.object(s, "_read_stream", AsyncMock(return_value=trace)):
            result = await s.performance_stop_trace()
        self.assertIn("1 events", result)
        self.assertEqual(self.a.handlers[uc.cdp.tracing.TracingComplete], [])
        self.b.send.assert_not_awaited()

    async def test_event_subscription_is_removed_without_touching_other_callbacks(self):
        def existing(event):
            pass

        self.a.add_handler(uc.cdp.runtime.ConsoleAPICalled, existing)
        tool = lambda name: s.mcp._tool_manager.get_tool(name).fn
        identifier = await tool("subscribe_cdp_events")(
            ["Runtime.consoleAPICalled"], enable_domain=False
        )
        event = uc.cdp.runtime.ConsoleAPICalled(
            "log",
            [],
            uc.cdp.runtime.ExecutionContextId(1),
            uc.cdp.runtime.Timestamp(1),
            None,
            None,
        )
        for callback in list(self.a.handlers[uc.cdp.runtime.ConsoleAPICalled]):
            callback(event)
        records = await tool("get_cdp_events")(identifier)
        self.assertEqual(records[0]["method"], "Runtime.consoleAPICalled")
        await tool("unsubscribe_cdp_events")(identifier)
        self.assertEqual(self.a.handlers[uc.cdp.runtime.ConsoleAPICalled], [existing])
