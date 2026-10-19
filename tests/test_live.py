"""Opt-in integration tests: NODRIVER_MCP_LIVE_TESTS=1 uv run python -m unittest discover -s tests."""

import asyncio
import json
import io
import logging
import os
import re
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import nodriver as uc
from mcp.server.mcpserver import Image
from mcp.server.mcpserver.exceptions import ToolError
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
        elif self.path.startswith("/interaction"):
            data, content_type = (
                b"""<title>Interaction regression</title>
                <input id="event-input" aria-label="Event input" value="old">
                <textarea id="event-textarea" aria-label="Event textarea">old</textarea>
                <button id="event-button">Event button</button>
                <script>
                window.formValues = {};
                window.controlledValues = {};
                window.changedValues = {};
                window.mouseEvents = [];
                window.keyEvents = [];
                for (const type of ['keydown', 'keyup', 'keypress']) {
                    document.addEventListener(type, event => {
                        window.keyEvents.push({type, key:event.key, code:event.code,
                            ctrl:event.ctrlKey, shift:event.shiftKey,
                            alt:event.altKey, meta:event.metaKey, trusted:event.isTrusted});
                        if (window.blockShortcuts && (event.ctrlKey || event.altKey || event.metaKey)) {
                            event.preventDefault();
                        }
                    });
                }
                for (const id of ['event-input', 'event-textarea']) {
                    const field = document.getElementById(id);
                    window.formValues[id] = field.value;
                    window.controlledValues[id] = field.value;
                    const descriptor = Object.getOwnPropertyDescriptor(
                        Object.getPrototypeOf(field), 'value'
                    );
                    let trackedValue = field.value;
                    Object.defineProperty(field, 'value', {
                        get() { return descriptor.get.call(this); },
                        set(value) {
                            trackedValue = String(value);
                            descriptor.set.call(this, value);
                        }
                    });
                    field.addEventListener('input', () => {
                        if (field.value !== trackedValue) {
                            window.controlledValues[id] = field.value;
                            trackedValue = field.value;
                        }
                    });
                    field.addEventListener('input', () => window.formValues[id] = field.value);
                    field.addEventListener('change', () => window.changedValues[id] = field.value);
                }
                for (const type of ['mousedown', 'mouseup', 'click', 'dblclick']) {
                    document.getElementById('event-button').addEventListener(type, event => {
                        window.mouseEvents.push({type, trusted:event.isTrusted, detail:event.detail, buttons:event.buttons});
                    });
                }
                </script>""",
                "text/html",
            )
        elif self.path.startswith("/actions/destination"):
            data, content_type = (
                b"<title>Destination</title><p id='ready'>Loading</p>"
                b"<script>setTimeout(() => ready.textContent='Destination ready', 60)</script>",
                "text/html",
            )
        elif self.path.startswith("/actions"):
            data, content_type = (
                b"""<title>Action waits</title><p id='result'>Original</p>
                <button onclick="setTimeout(() => result.textContent='Async ready', 40)">Async update</button>
                <a href='/actions/destination'>Navigate now</a>
                <button onclick="window.open('/actions/destination')">Open popup</button>
                <button onclick="window.busy=setInterval(() => result.textContent=String(Date.now()), 20)">Busy DOM</button>
                <button onclick="alert('manual dialog probe')">Open dialog</button>""",
                "text/html",
            )
        elif self.path.startswith("/toggle-child"):
            data, content_type = (
                b"<input type='checkbox' aria-label='Frame toggle'>",
                "text/html",
            )
        elif self.path.startswith("/toggles"):
            data, content_type = (
                b"""<title>Toggle controls</title>
                <input id='native' type='checkbox' aria-label='Native checkbox'>
                <input id='mixed' type='checkbox' aria-label='Mixed checkbox'>
                <input id='disabled' type='checkbox' disabled aria-label='Disabled checkbox'>
                <input id='radioA' type='radio' name='native-radio' aria-label='Native radio A'>
                <input id='radioB' type='radio' name='native-radio' aria-label='Native radio B'>
                <div id='aria' role='checkbox' tabindex='0' aria-label='ARIA checkbox' aria-checked='false'
                  onclick="this.setAttribute('aria-checked',this.getAttribute('aria-checked')==='true'?'false':'true')">custom</div>
                <div id='switch' role='switch' tabindex='0' aria-label='ARIA switch' aria-checked='false'
                  onclick="setTimeout(() => this.setAttribute('aria-checked',this.getAttribute('aria-checked')==='true'?'false':'true'),30)">switch</div>
                <div id='stuck' role='checkbox' tabindex='0' aria-label='Stuck checkbox' aria-checked='false'>stuck</div>
                <div id='ariaRadioA' role='radio' tabindex='0' aria-label='ARIA radio A' aria-checked='false'
                  onclick="ariaRadioA.setAttribute('aria-checked','true');ariaRadioB.setAttribute('aria-checked','false')">A</div>
                <div id='ariaRadioB' role='radio' tabindex='0' aria-label='ARIA radio B' aria-checked='false'
                  onclick="ariaRadioA.setAttribute('aria-checked','false');ariaRadioB.setAttribute('aria-checked','true')">B</div>
                <iframe src='/toggle-child'></iframe>
                <script>
                mixed.checked=true; mixed.indeterminate=true;
                window.toggleClicks=[];
                document.addEventListener('click', e => toggleClicks.push({id:e.target.id,trusted:e.isTrusted}));
                </script>""",
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
                "NODRIVER_PROXY": "",
                "NODRIVER_BROWSER_ARGS": '["--enable-blink-features=WebMCPSupport,WebMCPTesting"]'
                if "webmcp" in self._testMethodName
                else '["--site-per-process", "--disable-features="]'
                if "cross_process" in self._testMethodName
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

    async def navigate_frame(self, page, frame_id, url, separate):
        await page.send(
            uc.cdp.runtime.evaluate(
                f"document.querySelector('iframe').src = {json.dumps(url)}"
            )
        )
        async with asyncio.timeout(15):
            while True:
                tree = await page.get_frame_tree()
                listed = list(uc.util.flatten_frame_tree(tree))
                native = await page.get_frames()
                if (
                    any(
                        str(connection.target_id) == frame_id
                        and connection.target.url == url
                        for connection in native
                    )
                    if separate
                    else not native
                    and any(
                        str(frame.id_) == frame_id and frame.url == url
                        for frame in listed
                    )
                ):
                    return
                await asyncio.sleep(0.05)

    async def test_click_focuses_input_and_emits_trusted_mouse_events(self):
        await s.new_page(self.url + "interaction")
        snapshot = await s.take_snapshot()
        field = re.search(r'uid=(\S+) textbox "Event input"', snapshot).group(1)
        button = re.search(r'uid=(\S+) button "Event button"', snapshot).group(1)
        self.assertNotIn("Error", await s.click(field))
        tab = await s._active_tab()
        self.assertEqual(
            await s._evaluate(tab, "document.activeElement.id"), "event-input"
        )
        self.assertNotIn("Error", await s.click(button))
        events = await s._evaluate(tab, "window.mouseEvents")
        self.assertEqual(
            [event["type"] for event in events], ["mousedown", "mouseup", "click"]
        )
        self.assertTrue(all(event["trusted"] for event in events))
        await s._evaluate(tab, "window.mouseEvents = []")
        self.assertNotIn("Error", await s.click(button, dbl_click=True))
        events = await s._evaluate(tab, "window.mouseEvents")
        self.assertEqual(
            [event["type"] for event in events],
            [
                "mousedown",
                "mouseup",
                "click",
                "mousedown",
                "mouseup",
                "click",
                "dblclick",
            ],
        )
        self.assertEqual(
            [event["detail"] for event in events if event["type"] == "click"], [1, 2]
        )
        self.assertTrue(all(event["trusted"] for event in events))

    async def test_coordinate_clicks_emit_complete_trusted_single_and_double_sequences(
        self,
    ):
        await s.new_page(self.url + "interaction")
        tab = await s._active_tab()
        box = await s._evaluate(
            tab,
            "document.getElementById('event-button').getBoundingClientRect().toJSON()",
        )
        x, y = int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
        for double in (False, True):
            with self.subTest(double=double):
                await s._evaluate(tab, "window.mouseEvents=[]")
                result = await s.click_at(x, y, dbl_click=double, include_snapshot=True)
                self.assertIn("Event button", result)
                events = await s._evaluate(tab, "window.mouseEvents")
                expected = ["mousedown", "mouseup", "click"]
                if double:
                    expected = expected * 2 + ["dblclick"]
                self.assertEqual([event["type"] for event in events], expected)
                self.assertEqual(
                    [event["detail"] for event in events if event["type"] == "click"],
                    [1, 2] if double else [1],
                )
                self.assertTrue(all(event["trusted"] for event in events))
                self.assertTrue(
                    all(
                        event["buttons"] == 0
                        for event in events
                        if event["type"] == "mouseup"
                    )
                )

    async def test_keyboard_shortcuts_emit_modifiers_and_release_them(self):
        await s.new_page(self.url + "interaction")
        tab = await s._active_tab()
        for shortcut, target, code, flags in (
            ("Control+A", "A", "KeyA", {"ctrl"}),
            ("Control+Shift+x", "X", "KeyX", {"ctrl", "shift"}),
            ("Alt+x", "x", "KeyX", {"alt"}),
            ("Meta+b", "b", "KeyB", {"meta"}),
            ("Control++", "+", "Equal", {"ctrl"}),
            ("Control+Shift++", "+", "Equal", {"ctrl", "shift"}),
            ("Shift++", "+", "Equal", {"shift"}),
            ("Control", "Control", "ControlLeft", {"ctrl"}),
        ):
            with self.subTest(shortcut=shortcut):
                await s._evaluate(
                    tab,
                    "document.getElementById('event-input').focus(); window.blockShortcuts=true; window.keyEvents=[]",
                )
                self.assertNotIn("Error", await s.press_key(shortcut))
                events = await s._evaluate(tab, "window.keyEvents")
                down = next(
                    event
                    for event in events
                    if event["type"] == "keydown" and event["key"] == target
                )
                self.assertEqual(down["code"], code)
                self.assertEqual(
                    {flag for flag in ("ctrl", "shift", "alt", "meta") if down[flag]},
                    flags,
                )
                self.assertTrue(all(event["trusted"] for event in events))
                self.assertNotIn(
                    "keypress",
                    [
                        event["type"]
                        for event in events
                        if flags & {"ctrl", "alt", "meta"}
                    ],
                )
                await s._evaluate(tab, "window.keyEvents=[]")
                await s.press_key("b")
                released = (await s._evaluate(tab, "window.keyEvents"))[0]
                self.assertEqual(released["key"], "b")
                self.assertFalse(
                    any(released[flag] for flag in ("ctrl", "shift", "alt", "meta"))
                )

    async def test_keyboard_keys_edit_text_select_and_move_focus(self):
        await s.new_page(self.url + "interaction")
        tab = await s._active_tab()
        await s._evaluate(tab, "document.getElementById('event-input').focus()")
        await s.press_key(("Meta" if sys.platform == "darwin" else "Control") + "+A")
        selection = await s._evaluate(
            tab,
            "[document.activeElement.selectionStart, document.activeElement.selectionEnd]",
        )
        self.assertEqual(selection, [0, 3])
        await s.press_key("Backspace")
        self.assertEqual(await s._evaluate(tab, "document.activeElement.value"), "")
        for key in ("x", "Shift+a", "+", "Space", "Digit1", "Shift+Digit1"):
            await s.press_key(key)
        self.assertEqual(
            await s._evaluate(tab, "document.activeElement.value"), "xA+ 1!"
        )
        await s.press_key("Tab")
        self.assertEqual(
            await s._evaluate(tab, "document.activeElement.id"), "event-textarea"
        )
        await s.press_key("Enter")
        self.assertIn("\n", await s._evaluate(tab, "document.activeElement.value"))
        await s.press_key("Shift+Tab")
        self.assertEqual(
            await s._evaluate(tab, "document.activeElement.id"), "event-input"
        )
        await s._evaluate(tab, "document.activeElement.select()")
        await s.type_text("submitted", submit_key="Tab")
        self.assertEqual(
            await s._evaluate(tab, "document.getElementById('event-input').value"),
            "submitted",
        )
        self.assertEqual(
            await s._evaluate(tab, "document.activeElement.id"), "event-textarea"
        )

    async def test_parallel_pages_keep_input_uids_snapshots_and_images_separate(self):
        result = await s.new_page(self.url + "interaction")
        a = re.search(r"page_id=(\w+)", result).group(1)
        result = await s.new_page(self.url + "interaction", background=True)
        b = re.search(r"page_id=(\w+)", result).group(1)
        self.assertNotEqual(a, b)
        self.assertEqual(s._selected_target_id, a)
        await asyncio.gather(
            s.evaluate_script(
                "document.documentElement.style.background='rgb(255,0,0)'", page_id=a
            ),
            s.evaluate_script(
                "document.documentElement.style.background='rgb(0,0,255)'", page_id=b
            ),
        )
        snapshots = await asyncio.gather(
            s.take_snapshot(page_id=a), s.take_snapshot(page_id=b)
        )
        uids = [
            re.search(r'uid=(\S+) textbox "Event input"', text).group(1)
            for text in snapshots
        ]
        self.assertNotEqual(*uids)
        filled = await asyncio.gather(
            s.fill_form(
                [{"uid": uids[0], "value": "alpha"}], include_snapshot=True, page_id=a
            ),
            s.fill_form(
                [{"uid": uids[1], "value": "beta"}], include_snapshot=True, page_id=b
            ),
        )
        self.assertIn('value="alpha"', filled[0])
        self.assertNotIn('value="beta"', filled[0])
        self.assertIn('value="beta"', filled[1])
        self.assertNotIn('value="alpha"', filled[1])
        self.assertIn("stale", await s.fill(uids[0], "wrong-page", page_id=b))
        await asyncio.gather(
            s.type_text("!", submit_key="Tab", page_id=a),
            s.type_text("?", submit_key="Tab", page_id=b),
        )
        values = await asyncio.gather(
            *(
                s.evaluate_script(
                    "[document.getElementById('event-input').value,document.activeElement.id]",
                    page_id=page,
                )
                for page in (a, b)
            )
        )
        self.assertIn('"alpha!", "event-textarea"', values[0])
        self.assertIn('"beta?", "event-textarea"', values[1])
        images = await asyncio.gather(
            s.take_screenshot(page_id=a), s.take_screenshot(page_id=b)
        )
        colors = [
            PillowImage.open(io.BytesIO(img.data)).convert("RGB").getpixel((1, 1))
            for img in images
        ]
        self.assertEqual(colors, [(255, 0, 0), (0, 0, 255)])
        self.assertEqual(s._selected_target_id, a)

        pending = asyncio.create_task(
            s.evaluate_script(
                "new Promise(resolve => {window.finishConcurrent=resolve;})", page_id=a
            )
        )
        try:
            async with asyncio.timeout(5):
                while "function" not in await s.evaluate_script(
                    "typeof window.finishConcurrent", page_id=a
                ):
                    await asyncio.sleep(0.02)
            other = await asyncio.wait_for(
                s.evaluate_script(
                    "document.getElementById('event-input').value", page_id=b
                ),
                5,
            )
            self.assertIn("beta?", other)
            self.assertFalse(pending.done())
            await s.evaluate_script("window.finishConcurrent('completed-a')", page_id=a)
            self.assertIn("completed-a", await asyncio.wait_for(pending, 5))
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

        victim = str(self.browser.tabs[0].target_id)
        self.assertNotIn(victim, (a, b))
        self.assertIn("Page closed", await s.close_page(victim))
        self.assertIn(
            "alpha!",
            await s.evaluate_script(
                "document.getElementById('event-input').value", page_id=a
            ),
        )
        self.assertIn("Page closed", await s.close_page(a))
        with self.assertRaisesRegex(ValueError, "closed page_id"):
            await s.take_snapshot(page_id=a)
        self.assertIn(
            "beta?",
            await s.evaluate_script(
                "document.getElementById('event-input').value", page_id=b
            ),
        )

    async def test_parallel_pages_isolate_frames_navigation_and_emulation(self):
        a = s._selected_target_id
        result = await s.new_page(self.url, background=True)
        b = re.search(r"page_id=(\w+)", result).group(1)
        frames = await self.tool("list_frames", page_id=a)
        child = next(frame["frame_id"] for frame in frames if frame["parent_id"])
        await self.tool("select_frame", frame_id=child, page_id=a)
        snapshots = await asyncio.gather(
            s.take_snapshot(page_id=a), s.take_snapshot(page_id=b)
        )
        self.assertIn("Frame input", snapshots[0])
        self.assertNotIn("Main input", snapshots[0])
        self.assertIn("Main input", snapshots[1])
        found = await self.tool("find_elements", selector="input", page_id=a)
        self.assertEqual(len(found), 1)
        self.assertNotIn("Error", await s.fill(found[0]["uid"], "frame-a", page_id=a))
        await s.navigate_page(url=self.url + "interaction", page_id=b)
        self.assertEqual(s._selected_frames[a], child)
        self.assertIn("frame-a", await s.take_snapshot(page_id=a))
        await self.tool("select_frame", page_id=a)
        await asyncio.gather(
            s.emulate(viewport="420x720x1", page_id=a),
            s.emulate(viewport="800x600x1", page_id=b),
        )
        widths = await asyncio.gather(
            s.evaluate_script("innerWidth", page_id=a),
            s.evaluate_script("innerWidth", page_id=b),
        )
        self.assertIn("420", widths[0])
        self.assertIn("800", widths[1])
        self.assertEqual(s._selected_target_id, a)

    async def test_parallel_pages_keep_console_and_network_records_separate(self):
        a = s._selected_target_id
        result = await s.new_page(self.url, background=True)
        b = re.search(r"page_id=(\w+)", result).group(1)
        await asyncio.gather(
            s.enable_console_collection(page_id=a),
            s.enable_console_collection(page_id=b),
        )
        await asyncio.gather(
            *(
                s.evaluate_script(
                    f"(async () => {{console.log('owner-{label}'); await fetch('/api?owner={label}');}})()",
                    page_id=page,
                )
                for page, label in ((a, "alpha"), (b, "beta"))
            )
        )
        async with asyncio.timeout(5):
            while not all(
                any(
                    r["page_id"] == page and r["finished"] and "owner=" in r["url"]
                    for r in s._network_requests
                )
                for page in (a, b)
            ):
                await asyncio.sleep(0.02)
        for page, own, other in ((a, "alpha", "beta"), (b, "beta", "alpha")):
            console = await s.list_console_messages(page_id=page)
            network = await s.list_network_requests(url_filter="owner=", page_id=page)
            self.assertIn("owner-" + own, console)
            self.assertNotIn("owner-" + other, console)
            self.assertIn("owner=" + own, network)
            self.assertNotIn("owner=" + other, network)
            reqid = int(re.search(r"\[(\d+)\]", network).group(1))
            self.assertIn(
                "local-test", await s.get_network_request(reqid, page_id=page)
            )
            foreign = b if page == a else a
            self.assertIn(
                "No matching", await s.get_network_request(reqid, page_id=foreign)
            )
        await s.disable_console_collection(page_id=a)
        await s.evaluate_script("console.log('beta-still-active')", page_id=b)
        self.assertIn("beta-still-active", await s.list_console_messages(page_id=b))
        self.assertEqual(s._selected_target_id, a)

    async def test_action_waiter_observes_async_dom_navigation_and_popups(self):
        await s.new_page(self.url + "actions")
        page = s._selected_target_id
        snapshot = await s.take_snapshot()
        uid = re.search(r'uid=(\S+) button "Async update"', snapshot).group(1)
        result = await s.click(uid, include_snapshot=True)
        self.assertIn("Async ready", result)
        uid = re.search(r'uid=(\S+) button "Open popup"', result).group(1)
        result = await s.click(uid)
        popup = re.search(r"Opened page_ids: (\w+)", result).group(1)
        self.assertNotEqual(popup, page)
        self.assertEqual(s._selected_target_id, page)
        self.assertIn("Destination ready", await s.take_snapshot(page_id=popup))
        snapshot = await s.take_snapshot(page_id=page)
        uid = re.search(r'uid=(\S+) link "Navigate now"', snapshot).group(1)
        result = await s.click(uid, include_snapshot=True, page_id=page)
        self.assertIn("Destination ready", result)
        self.assertIn("Navigation observed", result)
        self.assertNotIn("Original", result)
        self.assertIn("stale", await s.click(uid, page_id=page))

    async def test_action_waiter_bounds_busy_dom_and_cleans_dialog_interruption(self):
        await s.new_page(self.url + "actions")
        page = await s._page_tab()
        events = (
            uc.cdp.page.FrameStartedNavigating,
            uc.cdp.page.FrameRequestedNavigation,
            uc.cdp.page.FrameNavigated,
            uc.cdp.page.FrameStoppedLoading,
            uc.cdp.page.JavascriptDialogOpening,
        )
        counts = {event: len(page.handlers.get(event, [])) for event in events}
        snapshot = await s.take_snapshot()
        uid = re.search(r'uid=(\S+) button "Busy DOM"', snapshot).group(1)
        start = asyncio.get_running_loop().time()
        result = await s.click(uid)
        self.assertIn("DOM stability wait reached", result)
        self.assertLess(asyncio.get_running_loop().time() - start, 6)
        await s.evaluate_script("clearInterval(window.busy)", wait_for_stable=False)
        snapshot = await s.take_snapshot()
        uid = re.search(r'uid=(\S+) button "Open dialog"', snapshot).group(1)
        result = await asyncio.wait_for(s.click(uid), 5)
        self.assertIn("interrupted by alert", result)
        await s.handle_dialog(action="dismiss")
        self.assertIn(
            "Action waits",
            await s.evaluate_script("document.title", wait_for_stable=False),
        )
        self.assertEqual(
            {event: len(page.handlers.get(event, [])) for event in events}, counts
        )

    async def test_toggle_fill_is_idempotent_trusted_and_checks_actual_state(self):
        await s.new_page(self.url + "toggles")
        tab = await s._active_tab()
        snapshot = await s.take_snapshot()
        uids = {
            name: uid
            for uid, _, name in re.findall(
                r'uid=(\S+) (checkbox|radio|switch) "([^"]+)"', snapshot
            )
        }
        for name, element_id in (
            ("Native checkbox", "native"),
            ("ARIA checkbox", "aria"),
            ("ARIA switch", "switch"),
        ):
            with self.subTest(control=name):
                self.assertNotIn("Error", await s.fill(uids[name], True))
                clicks = len(await s._evaluate(tab, "window.toggleClicks"))
                self.assertNotIn("Error", await s.fill(uids[name], "true"))
                self.assertEqual(
                    len(await s._evaluate(tab, "window.toggleClicks")), clicks
                )
                self.assertNotIn("Error", await s.fill(uids[name], "false"))
                state = await s._evaluate(
                    tab,
                    f"document.getElementById('{element_id}').checked"
                    if element_id == "native"
                    else f"document.getElementById('{element_id}').getAttribute('aria-checked')",
                )
                self.assertIn(state, (False, "false"))
        self.assertNotIn("Error", await s.fill(uids["Mixed checkbox"], True))
        self.assertEqual(
            await s._evaluate(tab, "[mixed.checked,mixed.indeterminate]"), [True, False]
        )
        self.assertIn("disabled", await s.fill(uids["Disabled checkbox"], True))
        self.assertNotIn("Error", await s.fill(uids["Disabled checkbox"], False))
        self.assertIn("did not reach", await s.fill(uids["Stuck checkbox"], True))
        self.assertIn("boolean", await s.fill(uids["Native checkbox"], "yes"))
        self.assertTrue(
            all(
                event["trusted"]
                for event in await s._evaluate(tab, "window.toggleClicks")
            )
        )
        with self.assertRaises(ToolError):
            await s.mcp.call_tool("fill", {"uid": uids["Native checkbox"], "value": 1})

    async def test_toggle_fill_form_and_radio_groups_preserve_selection_rules(self):
        await s.new_page(self.url + "toggles")
        tab = await s._active_tab()
        snapshot = await s.take_snapshot()
        uids = {
            name: uid
            for uid, _, name in re.findall(
                r'uid=(\S+) (checkbox|radio|switch) "([^"]+)"', snapshot
            )
        }
        result = await s.fill_form(
            [
                {"uid": uids["Native checkbox"], "value": True},
                {"uid": uids["ARIA switch"], "value": "true"},
            ],
            include_snapshot=True,
        )
        self.assertNotIn("Error", result)
        for label, prefix in (("Native", "radio"), ("ARIA", "ariaRadio")):
            self.assertNotIn("Error", await s.fill(uids[label + " radio A"], "true"))
            self.assertNotIn("Error", await s.fill(uids[label + " radio B"], True))
            self.assertNotIn("Error", await s.fill(uids[label + " radio A"], False))
            self.assertIn(
                "Cannot uncheck", await s.fill(uids[label + " radio B"], False)
            )
            expression = (
                f"[document.getElementById('{prefix}A').checked,document.getElementById('{prefix}B').checked]"
                if label == "Native"
                else "[ariaRadioA.getAttribute('aria-checked'),ariaRadioB.getAttribute('aria-checked')]"
            )
            self.assertEqual(
                await s._evaluate(tab, expression),
                [False, True] if label == "Native" else ["false", "true"],
            )

    async def test_cross_process_toggle_fill_uses_its_frame_connection(self):
        await s.new_page(self.url + "toggles")
        page = await s._page_tab()
        frames = await self.tool("list_frames")
        child = next(frame["frame_id"] for frame in frames if frame["parent_id"])
        await self.tool("select_frame", frame_id=child)
        found = await self.tool("find_elements", selector="input")
        self.assertNotIn("Error", await s.fill(found[0]["uid"], True))
        url = f"http://localhost:{self.http.server_port}/toggle-child"
        await self.navigate_frame(page, child, url, separate=True)
        found = await self.tool("find_elements", selector="input")
        self.assertNotIn("Error", await s.fill(found[0]["uid"], True))
        element = await s._element_for_uid(found[0]["uid"])
        self.assertIs(await element.apply("el => el.checked"), True)

    async def test_empty_fill_updates_input_and_textarea_form_models(self):
        await s.new_page(self.url + "interaction")
        snapshot = await s.take_snapshot()
        for label, field_id in (
            ("Event input", "event-input"),
            ("Event textarea", "event-textarea"),
        ):
            uid = re.search(rf'uid=(\S+) textbox "{label}"', snapshot).group(1)
            self.assertNotIn("Error", await s.fill(uid, ""))
            tab = await s._active_tab()
            state = await s._evaluate(
                tab,
                f"({{value:document.getElementById('{field_id}').value, "
                f"model:window.formValues['{field_id}'], "
                f"controlled:window.controlledValues['{field_id}'], "
                f"changed:window.changedValues['{field_id}']}})",
            )
            self.assertEqual(
                state, {"value": "", "model": "", "controlled": "", "changed": ""}
            )

    async def test_selecting_main_frame_id_allows_element_search(self):
        frames = await self.tool("list_frames")
        root = next(frame for frame in frames if frame["parent_id"] is None)
        await self.tool("select_frame", frame_id=root["frame_id"])
        for query in (
            {"selector": "input"},
            {"text": "Click probe", "best_match": True},
            {"xpath": "//button", "timeout": 0},
        ):
            with self.subTest(query=query):
                self.assertTrue(await self.tool("find_elements", **query))
        self.assertIn("Main input", await s.take_snapshot())
        self.assertEqual(
            await s._evaluate(
                await s._active_tab(),
                "document.querySelector('input').getAttribute('aria-label')",
            ),
            "Main input",
        )

    async def test_cross_process_frame_navigation_refreshes_connections(self):
        page = await s._page_tab()
        frames = await self.tool("list_frames")
        child = next(frame for frame in frames if frame["name"] == "child")
        frame_id = child["frame_id"]
        self.assertFalse(child["separate_connection"])
        await self.tool("select_frame", frame_id=frame_id)
        cross_url = f"http://localhost:{self.http.server_port}/frame?cross=1"

        await self.navigate_frame(page, frame_id, cross_url, True)
        frames = await self.tool("list_frames")
        child = next(frame for frame in frames if frame["frame_id"] == frame_id)
        self.assertTrue(child["separate_connection"])
        await self.tool("select_frame", frame_id=frame_id)
        connection = await s._active_tab()
        self.assertIsNot(connection, page)
        self.assertIn("Frame input", await s.take_snapshot())
        found = await self.tool("find_elements", selector="input")
        self.assertTrue(found)
        old_uid = found[0]["uid"]
        self.assertNotIn("Error", await s.click(old_uid))
        self.assertEqual(
            await s._evaluate(connection, "document.activeElement.tagName"), "INPUT"
        )
        self.assertNotIn("Error", await s.fill(old_uid, "cross-process"))
        self.assertIn(
            "cross-process",
            await s.evaluate_script("() => document.querySelector('input').value"),
        )
        await self.tool("list_frames")
        self.assertIs(await s._active_tab(), connection)

        await self.navigate_frame(page, frame_id, self.url + "frame?return=1", False)
        frames = await self.tool("list_frames")
        child = next(frame for frame in frames if frame["frame_id"] == frame_id)
        self.assertFalse(child["separate_connection"])
        self.assertIs(await s._active_tab(), page)
        self.assertIsNone(connection.socket)
        self.assertIn("Frame input", await s.take_snapshot())
        self.assertIn(
            "INPUT",
            await s.evaluate_script("() => document.querySelector('input').tagName"),
        )
        with self.assertRaisesRegex(ValueError, "stale"):
            await s._tab_for_uid(old_uid)

        current_uid = (await self.tool("find_elements", selector="input"))[0]["uid"]
        await self.navigate_frame(page, frame_id, self.url + "frame?return=2", False)
        with self.assertRaisesRegex(ValueError, "stale"):
            await s._tab_for_uid(current_uid)
        self.assertIs(await s._active_tab(), page)
        current_uid = (await self.tool("find_elements", selector="input"))[0]["uid"]
        self.assertNotIn("Error", await s.click(current_uid))
        self.assertEqual(
            await s._evaluate(page, "document.activeElement.tagName"), "INPUT"
        )
        await page.send(
            uc.cdp.runtime.evaluate("document.querySelector('iframe').remove()")
        )
        await self.tool("list_frames")
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            await s._active_tab()
        with self.assertRaisesRegex(ValueError, "stale"):
            await s._tab_for_uid(current_uid)
        self.assertIsNotNone(page.socket)

    async def test_cross_process_frame_collectors_survive_connection_recreation(self):
        page = await s._page_tab()
        child = next(
            frame
            for frame in await self.tool("list_frames")
            if frame["name"] == "child"
        )
        frame_id = child["frame_id"]
        await self.tool("select_frame", frame_id=frame_id)
        cross_url = f"http://localhost:{self.http.server_port}/frame?collect=1"

        async def collect_probe(connection, marker):
            self.assertNotIn("already", await s.enable_console_collection())
            await self.tool("configure_network_capture")
            # Enabling twice must preserve exactly one copy of each collector.
            self.assertIn("already", await s.enable_console_collection())
            await self.tool("configure_network_capture")
            await s._evaluate(
                connection,
                f"(async () => {{ console.log('{marker}'); "
                f"return (await fetch('/api?probe={marker}')).json(); }})()",
            )
            async with asyncio.timeout(5):
                while not (
                    any(marker in message["text"] for message in s._console_messages)
                    and any(
                        marker in request["url"]
                        and request["connection"] is connection
                        and request["finished"]
                        for request in s._network_requests
                    )
                ):
                    await asyncio.sleep(0.01)
            self.assertEqual(
                len(connection.handlers[uc.cdp.runtime.ConsoleAPICalled]), 1
            )
            for event in (
                uc.cdp.network.RequestWillBeSent,
                uc.cdp.network.ResponseReceived,
                uc.cdp.network.LoadingFinished,
                uc.cdp.network.LoadingFailed,
            ):
                self.assertEqual(len(connection.handlers[event]), 1)
            self.assertEqual(
                sum(marker in message["text"] for message in s._console_messages), 1
            )
            requests = [
                request
                for request in s._network_requests
                if marker in request["url"] and request["connection"] is connection
            ]
            self.assertEqual(len(requests), 1)
            self.assertIn(marker, await s.list_console_messages())
            self.assertIn(marker, await s.list_network_requests(url_filter=marker))
            self.assertIn(
                "local-test", await s.get_network_request(requests[0]["reqid"])
            )

        await self.navigate_frame(page, frame_id, cross_url, True)
        old = await s._active_tab()
        self.assertIsNot(old, page)
        await collect_probe(old, "collection-before-recreation")

        await self.navigate_frame(page, frame_id, self.url + "frame?collect=2", False)
        self.assertIs(await s._active_tab(), page)
        self.assertIsNone(old.socket)

        await self.navigate_frame(page, frame_id, cross_url, True)
        replacement = await s._active_tab()
        self.assertIsNot(replacement, old)
        self.assertEqual(str(replacement.target_id), str(old.target_id))
        await collect_probe(replacement, "collection-after-recreation")
        self.assertNotIn(s._tab_key(old), s._console_collection_enabled_tabs)
        self.assertNotIn(s._tab_key(old), s._network_collection_enabled_tabs)
        self.assertNotIn(s._tab_key(old), s._console_callbacks)
        self.assertNotIn(s._tab_key(old), s._network_callbacks)
        self.assertFalse(old.handlers.get(uc.cdp.runtime.ConsoleAPICalled))
        self.assertFalse(old.handlers.get(uc.cdp.network.RequestWillBeSent))

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
        result = await s.mcp.call_tool("take_screenshot", {})
        self.assertFalse(result.is_error)
        self.assertTrue(any(item.type == "image" for item in result.content))
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
