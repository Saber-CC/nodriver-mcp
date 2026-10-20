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
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

import anyio
import nodriver as uc
from mcp.server.mcpserver import Image
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image as PillowImage

from nodriver_mcp import server as s


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith(("/early-frame", "/echo-headers?early=")):
            self.server.early_requests.append((self.path, dict(self.headers)))
        if self.path.startswith("/identity-"):
            parsed = urlsplit(self.path)
            label = parse_qs(parsed.query)["name"][0]
            if parsed.path == "/identity-page":
                data = f'<script src="/identity-script?name={label}"></script>'.encode()
                content_type = "text/html"
            elif parsed.path == "/identity-script":
                data = (
                    f'function minified(){{console.log("identity-{label}")}};minified();\n'
                    f"//# sourceMappingURL=/identity-map?name={label}"
                ).encode()
                content_type = "application/javascript"
            else:
                data = json.dumps(
                    {
                        "version": 3,
                        "sources": [label + ".ts"],
                        "names": [],
                        "mappings": "AAAA",
                        "sourcesContent": [f'console.log("source-{label}");'],
                    }
                ).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/mapped.js.map"):
            headers = getattr(self.server, "source_map_headers", [])
            headers.append(dict(self.headers))
            self.server.source_map_headers = headers
            if "map_access=yes" not in self.headers.get("Cookie", ""):
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            data = json.dumps(
                {
                    "version": 3,
                    "sources": ["original.ts"],
                    "sourceRoot": "/src/",
                    "names": ["originalLogger"],
                    "mappings": "AAIEA",
                    "sourcesContent": ['1\n2\n3\n4\n  console.error("original line");'],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/mapped.js"):
            data = b"function deployedCrash(){console.error('mapped-console-marker');setTimeout(function minifiedAsync(){throw new Error('mapped-exception-marker')},10)}\n//# sourceMappingURL=/mapped.js.map"
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/net-redirect"):
            destination = (
                "/net-final" if "second" in self.path else "/net-redirect?second"
            )
            self.send_response(302)
            self.send_header("Location", destination)
            self.send_header(
                "Set-Cookie",
                "hop=" + ("second" if "second" in self.path else "first") + "; Path=/",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/early-frame-asset.js"):
            data, content_type = (
                (
                    "window.assetHeader="
                    + json.dumps(self.headers.get("X-Review"))
                    + ";"
                ).encode(),
                "application/javascript",
            )
        elif self.path.startswith("/early-frame"):
            marker = parse_qs(urlsplit(self.path).query)["early"][0]
            data, content_type = (
                (
                    "<script>window.initialMotion=matchMedia('(prefers-reduced-motion:reduce)').matches;</script>"
                    f"<script src='/early-frame-asset.js?early={marker}'></script>"
                    f"<script>window.initialFetch=fetch('/echo-headers?early={marker}').then(r=>r.json());</script>"
                ).encode(),
                "text/html",
            )
        elif self.path.startswith("/echo-headers"):
            data, content_type = (
                json.dumps(dict(self.headers)).encode(),
                "application/json",
            )
        elif self.path.startswith("/net-final"):
            data, content_type = b"redirect-complete", "text/plain"
        elif self.path.startswith("/api"):
            data, content_type = (
                b'{"ok":true,"source":"local-test"}',
                "application/json",
            )
        elif self.path.startswith("/frame"):
            data, content_type = (
                b'<input aria-label="Frame input"><button>Frame button</button>',
                "text/html",
            )
        elif self.path.startswith("/nested-outer"):
            data, content_type = (
                b"<script>window.frameLevel='outer'</script>"
                b"<iframe name='nested-child' src='/nested-inner'></iframe>",
                "text/html",
            )
        elif self.path.startswith("/nested-inner"):
            data, content_type = (
                b"<script>window.frameLevel='inner';let frameLexical=42;</script>"
                b"<button>Nested inner marker</button>"
                b"<input aria-label='Nested input'>",
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
                window.inputEvents = [];
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
                    field.addEventListener('input', event => inputEvents.push({id, trusted:event.isTrusted}));
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
        elif self.path.startswith(("/uploads", "/upload-child")):
            data, content_type = (
                b"""<title>Uploads</title>
                <input id='one' type='file' aria-label='Single upload'>
                <input id='many' type='file' multiple aria-label='Multiple upload'>
                <input id='hidden' type='file' multiple style='display:none'>
                <button id='choose' onclick="document.getElementById('hidden').click()">Choose files</button>
                <button id='none'>No chooser</button><output id='names'>No files</output>
                <script>window.filesChanged={}; document.addEventListener('change', event => {
                  if (event.target.type !== 'file') return;
                  const files=Array.from(event.target.files, file=>file.name);
                  filesChanged[event.target.id]=files;
                  document.getElementById('names').textContent=files.join(', ');
                });</script>"""
                + (
                    b"<iframe src='/upload-child'></iframe>"
                    if self.path.startswith("/uploads")
                    else b""
                ),
                "text/html",
            )
        elif self.path.startswith("/errors.js"):
            data, content_type = (
                b"""window.emitErrors = function emitErrors() {
                  console.warn('stack-marker');
                  setTimeout(function asyncCrash() { throw new Error('async-exception-marker'); }, 10);
                  Promise.reject(new Error('promise-exception-marker'));
                  return 'scheduled';
                };""",
                "application/javascript",
            )
        elif self.path.startswith("/performance"):
            data, content_type = (
                b"""<meta charset='utf-8'><title>Performance integration</title>
                <style>body{margin:0;background:white}h1{font-size:48px}button{width:200px;height:60px}</style>
                <h1 id='hero'>Performance content</h1><button id='traceAction'>Trace action</button>
                <script>window.traceClicks=0;traceAction.onclick=()=>{const start=performance.now();while(performance.now()-start<160){}traceClicks++;traceAction.textContent='Trace done';};
                setTimeout(()=>hero.style.marginTop='160px',250);</script>""",
                "text/html",
            )
        elif self.path.startswith("/console-errors"):
            data, content_type = (
                b"<title>Console stack</title><script src='/errors.js'></script><p>Exception test</p>",
                "text/html",
            )
        elif self.path.startswith("/form-batch"):
            data, content_type = (
                b"""<title>Batch form</title>
                <input id='first' value='old'><input id='second' value='untouched'><input id='third' value='last'>
                <div id='edit' role='textbox' contenteditable='plaintext-only'>old</div>
                <input id='readonly' readonly value='keep'><input id='disabled' disabled value='keep'>
                <input id='limited' maxlength='3' value='old'><input id='date' type='date' value='2026-01-01'>
                <button id='action'>Action</button>
                <script>
                window.batchMode=''; window.batchEvents=[];
                document.addEventListener('input', event => {
                  batchEvents.push(event.target.id); sessionStorage.setItem('batchEvents', JSON.stringify(batchEvents));
                  if (event.target.id === 'second') {
                    if (batchMode === 'dialog') alert('batch interrupted');
                    if (batchMode === 'navigate') location.href='/actions/destination';
                  }
                });
                window.editModel='old'; document.getElementById('edit').addEventListener('input', event=>window.editModel=event.target.innerText);
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
        if self.path.startswith("/strict-csp"):
            self.send_header(
                "Content-Security-Policy", "default-src 'none'; script-src 'none'"
            )
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
        s._browser_lock = asyncio.Lock()
        self.directory = tempfile.TemporaryDirectory(prefix="nodriver-mcp-integration-")
        self.env = patch.dict(
            os.environ,
            {
                "NODRIVER_HEADLESS": "true",
                "NODRIVER_USER_DATA_DIR": self.directory.name,
                "NODRIVER_EXTENSIONS": "[]",
                "NODRIVER_ARTIFACTS_DIR": str(Path(self.directory.name) / "artifacts"),
                "NODRIVER_PROXY": "",
                "NODRIVER_BROWSER_ARGS": '["--enable-unsafe-extension-debugging"]'
                if "service_worker" in self._testMethodName
                else '["--enable-blink-features=WebMCPSupport,WebMCPTesting"]'
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

    async def test_browser_version_details_do_not_leave_pages_or_change_selection(self):
        await self.browser.update_targets()
        before = {str(tab.target_id) for tab in self.browser.tabs}
        selected = s._selected_target_id
        for _ in range(2):
            info = await self.tool("get_browser_info", include_version_page=True)
            details = info["version_page"]
            self.assertTrue(details)
            version = info["product"].split("/", 1)[1]
            self.assertTrue(any(version in value for value in details.values()))
            self.assertTrue(
                any("--remote-debugging-port=" in value for value in details.values())
            )
            await self.browser.update_targets()
            self.assertEqual({str(tab.target_id) for tab in self.browser.tabs}, before)
            self.assertEqual(s._selected_target_id, selected)
            self.assertIn("Main input", await s.take_snapshot())

    async def test_navigation_and_new_page_report_final_urls_after_redirects(self):
        expected = self.url + "net-final"
        opened = await s.new_page(self.url + "net-redirect")
        page_id = s._selected_target_id
        self.assertEqual(
            opened.splitlines()[0],
            f"Opened new page: {expected} (page_id={page_id})",
        )
        for options, destination in (
            ({"url": self.url + "interaction"}, self.url + "interaction"),
            ({"type": "back"}, expected),
            ({"type": "forward"}, self.url + "interaction"),
            ({"type": "reload"}, self.url + "interaction"),
            ({"url": self.url + "net-redirect"}, expected),
        ):
            with self.subTest(options=options):
                result = await s.navigate_page(**options, page_id=page_id)
                self.assertEqual(result.splitlines()[0], f"Navigated to {destination}")
        background = await s.new_page(self.url + "net-redirect", background=True)
        self.assertTrue(
            background.splitlines()[0].startswith(f"Opened new page: {expected} ")
        )
        self.assertEqual(s._selected_target_id, page_id)

    async def test_browser_version_page_cleanup_after_read_failure_and_cancellation(
        self,
    ):
        await self.browser.update_targets()
        before = {str(tab.target_id) for tab in self.browser.tabs}
        selected = s._selected_target_id
        evaluate = uc.cdp.runtime.evaluate
        with patch.object(
            uc.cdp.runtime, "evaluate", side_effect=ValueError("Version read failed")
        ):
            with self.assertRaisesRegex(ValueError, "Version read failed"):
                await self.tool("get_browser_info", include_version_page=True)
        await self.browser.update_targets()
        self.assertEqual({str(tab.target_id) for tab in self.browser.tabs}, before)

        entered = asyncio.Event()

        def pending(*_args, **_kwargs):
            entered.set()
            return evaluate("new Promise(() => {})", await_promise=True)

        with patch.object(uc.cdp.runtime, "evaluate", side_effect=pending):
            task = asyncio.create_task(
                self.tool("get_browser_info", include_version_page=True)
            )
            try:
                await asyncio.wait_for(entered.wait(), 5)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        await self.browser.update_targets()
        self.assertEqual({str(tab.target_id) for tab in self.browser.tabs}, before)
        self.assertEqual(s._selected_target_id, selected)
        self.assertIn("Main input", await s.take_snapshot())

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

    async def test_same_email_fill_keeps_focus_and_does_not_interrupt_batch(self):
        page = await s._page_tab()
        await s._evaluate(
            page,
            """document.body.insertAdjacentHTML('beforeend',
            '<input id="email" type="email" aria-label="Repeat email" value="old@example.test">');""",
        )
        snapshot = await s.take_snapshot()
        email = re.search(r'uid=(\S+) textbox "Repeat email"', snapshot).group(1)
        other = re.search(r'uid=(\S+) textbox "Main input"', snapshot).group(1)
        self.assertNotIn("Error", await s.fill(email, "old@example.test"))
        self.assertEqual(await s._evaluate(page, "document.activeElement.id"), "email")
        report = await s.fill_form(
            [
                {"uid": email, "value": "old@example.test"},
                {"uid": other, "value": "batch continues"},
            ]
        )
        self.assertIn("Completed 2 of 2 fields", report)
        self.assertNotIn("Error", report)
        self.assertNotIn("Error", await s.fill(email, "next@example.test"))
        self.assertEqual(
            await s._evaluate(page, "document.getElementById('email').value"),
            "next@example.test",
        )

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

    async def test_cross_process_initial_iframe_scripts_and_resources_inherit_overrides(
        self,
    ):
        page = await s._page_tab()
        self.http.early_requests = []
        await s.emulate(
            prefers_reduced_motion="reduce", extra_http_headers={"X-Review": "first"}
        )
        for number, header in enumerate(("first", "updated")):
            if number:
                await s.emulate(extra_http_headers={"X-Review": header})
            url = (
                self.url.replace("127.0.0.1", "localhost")
                + f"early-frame?early={number}"
            )
            # No list_frames/snapshot call may initialize the new connection.
            with patch.object(
                s,
                "_discover_frames",
                side_effect=AssertionError("late frame discovery"),
            ):
                await s._evaluate(
                    page,
                    """new Promise(resolve => {
                    const frame=document.createElement('iframe');frame.id='early-%s';
                    frame.onload=()=>resolve(true);frame.src=%s;document.body.append(frame);
                })"""
                    % (number, json.dumps(url)),
                )
            native = next(c for c in await page.get_frames() if c.target.url == url)
            try:
                result, error = await native.send(
                    uc.cdp.runtime.evaluate(
                        "(async()=>({motion:initialMotion,asset:assetHeader,headers:await initialFetch}))()",
                        return_by_value=True,
                        await_promise=True,
                    )
                )
                self.assertIsNone(error)
                self.assertEqual(
                    result.value,
                    {
                        "motion": True,
                        "asset": header,
                        "headers": result.value["headers"],
                    },
                )
                self.assertEqual(result.value["headers"].get("X-Review"), header)
            finally:
                await native.aclose()
            requests = [
                (p, h)
                for p, h in self.http.early_requests
                if p.endswith(f"early={number}")
            ]
            self.assertEqual(len(requests), 3, requests)
            self.assertTrue(
                all(h.get("X-Review") == header for p, h in requests), requests
            )
        await s.close_page(str(page.target_id))
        self.assertFalse(s._frame_overrides.pages)

    async def test_cross_process_media_and_headers_follow_frame_connections(self):
        page = await s._page_tab()
        frames = await s._discover_frames(page)
        frame = next(f for f, c in frames if f.parent_id is not None)
        frame_id = str(frame.id_)
        await s.emulate(
            prefers_reduced_motion="reduce", extra_http_headers={"X-Frame": "first"}
        )
        cross = self.url.replace("127.0.0.1", "localhost") + "frame"
        await self.navigate_frame(page, frame_id, cross, True)
        await self.tool("select_frame", frame_id=frame_id)
        self.assertIn(
            "true",
            await s.evaluate_script(
                "matchMedia('(prefers-reduced-motion:reduce)').matches",
                wait_for_stable=False,
            ),
        )
        self.assertIn(
            "first",
            await s.evaluate_script(
                "fetch('/echo-headers').then(r=>r.json())", wait_for_stable=False
            ),
        )
        await s.emulate(extra_http_headers={"X-Frame": "next"})
        await self.tool("select_frame")
        await self.navigate_frame(page, frame_id, self.url + "frame", False)
        await s._discover_frames(page)
        await self.navigate_frame(page, frame_id, cross, True)
        await self.tool("select_frame", frame_id=frame_id)
        self.assertIn(
            "next",
            await s.evaluate_script(
                "fetch('/echo-headers').then(r=>r.json())", wait_for_stable=False
            ),
        )
        await s.reset_emulation()
        self.assertIn(
            "false",
            await s.evaluate_script(
                "matchMedia('(prefers-reduced-motion:reduce)').matches",
                wait_for_stable=False,
            ),
        )
        self.assertNotIn(
            "X-Frame",
            await s.evaluate_script(
                "fetch('/echo-headers').then(r=>r.json())", wait_for_stable=False
            ),
        )

    async def test_media_and_headers_merge_persist_and_reset_per_page(self):
        a = s._selected_target_id
        opened = await s.new_page(self.url, background=True)
        b = re.search(r"page_id=(\w+)", opened).group(1)
        await asyncio.gather(
            s.emulate(
                color_scheme="dark",
                forced_colors="active",
                prefers_contrast="more",
                prefers_reduced_motion="reduce",
                extra_http_headers={"X-Owner": "alpha"},
                page_id=a,
            ),
            s.emulate(
                forced_colors="none",
                prefers_contrast="less",
                extra_http_headers='{"X-Owner":"beta"}',
                page_id=b,
            ),
        )
        query = "({dark:matchMedia('(prefers-color-scheme:dark)').matches,forced:matchMedia('(forced-colors:active)').matches,more:matchMedia('(prefers-contrast:more)').matches,less:matchMedia('(prefers-contrast:less)').matches,reduce:matchMedia('(prefers-reduced-motion:reduce)').matches})"
        self.assertEqual(
            json.loads(
                (
                    await s.evaluate_script(query, wait_for_stable=False, page_id=a)
                ).splitlines()[1]
            ),
            {"dark": True, "forced": True, "more": True, "less": False, "reduce": True},
        )
        await s.emulate(forced_colors="auto", prefers_contrast="less", page_id=a)
        values = json.loads(
            (
                await s.evaluate_script(query, wait_for_stable=False, page_id=a)
            ).splitlines()[1]
        )
        self.assertTrue(values["dark"])
        self.assertTrue(values["reduce"])
        self.assertTrue(values["less"])
        for page, owner in ((a, "alpha"), (b, "beta")):
            await s.navigate_page(url=self.url + "interaction", page_id=page)
            headers = json.loads(
                (
                    await s.evaluate_script(
                        "fetch('/echo-headers').then(r=>r.json())",
                        wait_for_stable=False,
                        page_id=page,
                    )
                ).splitlines()[1]
            )
            self.assertEqual(headers["X-Owner"], owner)
        await s.reset_emulation(page_id=a)
        reset = json.loads(
            (
                await s.evaluate_script(query, wait_for_stable=False, page_id=a)
            ).splitlines()[1]
        )
        self.assertFalse(reset["forced"])
        self.assertFalse(reset["reduce"])
        self.assertFalse(reset["more"])
        cleared = await s.evaluate_script(
            "fetch('/echo-headers').then(r=>r.json())", wait_for_stable=False, page_id=a
        )
        self.assertNotIn("X-Owner", cleared)
        self.assertIn(
            "beta",
            await s.evaluate_script(
                "fetch('/echo-headers').then(r=>r.json())",
                wait_for_stable=False,
                page_id=b,
            ),
        )
        await s.emulate(extra_http_headers="", page_id=b)
        self.assertNotIn(
            "X-Owner",
            await s.evaluate_script(
                "fetch('/echo-headers').then(r=>r.json())",
                wait_for_stable=False,
                page_id=b,
            ),
        )
        self.assertIn(
            "true",
            await s.evaluate_script(
                "document.hasFocus()", wait_for_stable=False, page_id=a
            ),
        )
        with self.assertRaisesRegex(ValueError, "forced_colors"):
            await s.emulate(forced_colors="invalid", page_id=a)

    async def test_background_page_focus_survives_selection_navigation_and_reset(self):
        a = s._selected_target_id
        opened = await s.new_page(self.url + "interaction", background=True)
        b = re.search(r"page_id=(\w+)", opened).group(1)
        for page in (a, b):
            self.assertIn(
                "true",
                await s.evaluate_script(
                    "document.hasFocus()", wait_for_stable=False, page_id=page
                ),
            )
        await s.select_page(b, bring_to_front=False)
        await s.navigate_page(url=self.url + "interaction", page_id=a)
        await s.reset_emulation(page_id=a)
        for page in (a, b):
            self.assertIn(
                "true",
                await s.evaluate_script(
                    "document.hasFocus()", wait_for_stable=False, page_id=page
                ),
            )
        self.assertEqual(s._selected_target_id, b)

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

    async def test_file_upload_supports_legacy_multiple_and_button_chooser(self):
        await s.new_page(self.url + "uploads")
        tab = await s._active_tab()
        first, second = (
            Path(self.directory.name) / "alpha.txt",
            Path(self.directory.name) / "样例.txt",
        )
        first.write_text("alpha", encoding="utf-8")
        second.write_text("unicode", encoding="utf-8")

        async def uid(selector):
            return (await self.tool("find_elements", selector=selector))[0]["uid"]

        single = await uid("#one")
        result = await s.upload_file(single, str(first), include_snapshot=True)
        self.assertIn("alpha.txt", result)
        single = await uid("#one")
        with self.assertRaisesRegex(ValueError, "multiple"):
            await s.upload_file(single, file_paths=[str(first), str(second)])
        self.assertEqual(
            await s._evaluate(tab, "document.getElementById('one').files.length"), 1
        )
        multiple = await uid("#many")
        result = await s.upload_file(
            multiple, file_paths=[str(first), str(second)], include_snapshot=True
        )
        self.assertIn("样例.txt", result)
        self.assertEqual(
            await s._evaluate(tab, "window.filesChanged.many"),
            ["alpha.txt", "样例.txt"],
        )
        await s.evaluate_script(
            "document.getElementById('hidden').multiple=false", wait_for_stable=False
        )
        button = await uid("#choose")
        with self.assertRaisesRegex(ValueError, "multiple"):
            await s.upload_file(button, file_paths=[str(first), str(second)])
        await s.evaluate_script(
            "document.getElementById('hidden').multiple=true", wait_for_stable=False
        )
        result = await s.upload_file(
            button, file_paths=[str(first), str(second)], include_snapshot=True
        )
        self.assertIn("alpha.txt", result)
        self.assertEqual(
            await s._evaluate(tab, "window.filesChanged.hidden"),
            ["alpha.txt", "样例.txt"],
        )
        self.assertFalse(tab.handlers.get(uc.cdp.page.FileChooserOpened))
        with self.assertRaisesRegex(ValueError, "does not exist"):
            await s.upload_file(single, file_path=str(first) + ".missing")
        with self.assertRaisesRegex(ValueError, "not both"):
            await s.upload_file(single, str(first), file_paths=[str(second)])

    async def test_file_upload_chooser_timeout_cleans_up_for_next_upload(self):
        await s.new_page(self.url + "uploads")
        tab = await s._active_tab()
        file = Path(self.directory.name) / "recovered.txt"
        file.write_text("file", encoding="utf-8")
        uid = (await self.tool("find_elements", selector="#none"))[0]["uid"]
        with self.assertRaisesRegex(TimeoutError, "file chooser"):
            await s.upload_file(uid, str(file), timeout=100)
        self.assertFalse(tab.handlers.get(uc.cdp.page.FileChooserOpened))
        uid = (await self.tool("find_elements", selector="#choose"))[0]["uid"]
        await s.upload_file(uid, file_paths=[str(file)])
        self.assertEqual(
            await s._evaluate(tab, "window.filesChanged.hidden"), ["recovered.txt"]
        )
        self.assertFalse(s._file_uploads.leases)

    async def test_cross_process_file_chooser_routes_to_iframe_input(self):
        await s.new_page(self.url + "uploads")
        page = await s._page_tab()
        frames = await self.tool("list_frames")
        child = next(frame["frame_id"] for frame in frames if frame["parent_id"])
        await self.tool("select_frame", frame_id=child)
        file = Path(self.directory.name) / "frame.txt"
        file.write_text("frame", encoding="utf-8")
        uid = (await self.tool("find_elements", selector="#one"))[0]["uid"]
        await s.upload_file(uid, str(file))
        url = f"http://localhost:{self.http.server_port}/upload-child"
        await self.navigate_frame(page, child, url, separate=True)
        uid = (await self.tool("find_elements", selector="#choose"))[0]["uid"]
        await s.upload_file(uid, str(file))
        connection = await s._active_tab()
        self.assertIsNot(connection, page)
        self.assertEqual(
            await s._evaluate(connection, "window.filesChanged.hidden"), ["frame.txt"]
        )
        for connection in (page, connection):
            self.assertFalse(connection.handlers.get(uc.cdp.page.FileChooserOpened))

    async def test_console_collects_uncaught_errors_promise_rejections_and_stacks(self):
        await s.new_page(self.url + "console-errors")
        page = await s._page_tab()
        self.assertFalse(page.handlers.get(uc.cdp.runtime.ExceptionThrown))
        await s.enable_console_collection()
        self.assertIn(
            "scheduled",
            await s.evaluate_script("window.emitErrors()", wait_for_stable=False),
        )
        async with asyncio.timeout(5):
            while not all(
                any(marker in m["text"] for m in s._console_messages)
                for marker in ("async-exception-marker", "promise-exception-marker")
            ):
                await asyncio.sleep(0.02)
        compact = await s.list_console_messages(types=["error"])
        self.assertIn("async-exception-marker", compact)
        self.assertIn("promise-exception-marker", compact)
        self.assertNotIn("stack-marker", compact)
        self.assertNotIn("at asyncCrash", compact)
        verbose = await s.list_console_messages(include_stack_traces=True)
        self.assertIn("errors.js:", verbose)
        self.assertIn("emitErrors", verbose)
        errors = [m for m in s._console_messages if m.get("source") == "exception"]
        for error in errors:
            self.assertEqual(error["location"]["url"], self.url + "errors.js")
            self.assertIsInstance(error["location"]["line_number"], int)
            self.assertIsNotNone(error["execution_context_id"])
            self.assertIn("Stack trace", await s.get_console_message(error["msgid"]))
            self.assertNotIn(
                "Stack trace",
                await s.get_console_message(error["msgid"], include_stack_traces=False),
            )
        await s.navigate_page(url=self.url + "interaction")
        self.assertIn(
            "async-exception-marker",
            await s.list_console_messages(include_preserved_messages=True),
        )
        self.assertIn("errors.js", await s.get_console_message(errors[0]["msgid"]))
        await s.disable_console_collection()
        count = len(s._console_messages)
        await s.evaluate_script("console.warn('after-disable')", wait_for_stable=False)
        self.assertEqual(len(s._console_messages), count)
        self.assertFalse(page.handlers.get(uc.cdp.runtime.ExceptionThrown))
        self.assertFalse(page.handlers.get(uc.cdp.runtime.ExceptionRevoked))

    async def test_script_dialog_policies_handle_sync_async_and_repeated_dialogs(self):
        page = await s._page_tab()
        count = len(page.handlers[uc.cdp.page.JavascriptDialogOpening])
        for source, options, expected in (
            (
                "() => {alert('accepted alert');return confirm('accepted confirm');}",
                {},
                "true",
            ),
            (
                "() => confirm('dismissed confirm')",
                {"dialog_action": "dismiss"},
                "false",
            ),
            (
                "() => prompt('prompt content','default')",
                {"prompt_text": "hello 中文"},
                "hello 中文",
            ),
            ("() => prompt('dismissed prompt')", {"dialog_action": "dismiss"}, "null"),
            (
                "async () => {await new Promise(r=>setTimeout(r,20));alert('async alert');return 'async ok';}",
                {},
                "async ok",
            ),
            (
                "() => {for(let i=0;i<3;i++)alert('same');return 'three done';}",
                {},
                "three done",
            ),
        ):
            with self.subTest(source=source):
                result = await asyncio.wait_for(
                    s.evaluate_script(source, wait_for_stable=False, **options), 5
                )
                self.assertIn("Status: completed", result)
                self.assertIn(expected, result)
                self.assertFalse(s._dialog_state.current)
        self.assertEqual(len(page.handlers[uc.cdp.page.JavascriptDialogOpening]), count)

    async def test_script_manual_dialog_interrupts_and_handles_the_same_page(self):
        a = s._selected_target_id
        opened = await s.new_page(self.url + "interaction", background=True)
        b = re.search(r"page_id=(\w+)", opened).group(1)
        result = await asyncio.wait_for(
            s.evaluate_script(
                "() => {window.afterManual=confirm('manual confirmation');return window.afterManual;}",
                dialog_action="manual",
                page_id=a,
            ),
            5,
        )
        self.assertIn("Status: interrupted", result)
        self.assertIn("manual confirmation", result)
        with self.assertRaisesRegex(ValueError, "manual confirmation"):
            await asyncio.wait_for(s.take_snapshot(page_id=a), 2)
        self.assertIn(
            "Status: completed",
            await s.evaluate_script("document.title", wait_for_stable=False, page_id=b),
        )
        await s.handle_dialog(action="accept", page_id=a)
        self.assertIn(
            "true",
            await s.evaluate_script(
                "window.afterManual", wait_for_stable=False, page_id=a
            ),
        )
        self.assertFalse(s._dialog_state.current)

    async def test_script_statuses_distinguish_timeout_failure_and_success(self):
        page = await s._page_tab()
        result = await asyncio.wait_for(
            s.evaluate_script(
                "new Promise(() => {})", timeout=100, wait_for_stable=False
            ),
            3,
        )
        self.assertIn("Status: timed_out", result)
        self.assertIn("Script execution", result)
        failed = await s.evaluate_script(
            "() => {throw new Error('failed-marker');}", wait_for_stable=False
        )
        self.assertIn("Status: failed", failed)
        self.assertIn("failed-marker", failed)
        self.assertIn(
            "Status: completed", await s.evaluate_script("21*2", wait_for_stable=False)
        )
        self.assertFalse(page._mapper)
        self.assertFalse(s._dialog_state.current)

    async def test_cross_process_script_dialog_policies_use_the_frame_owner(self):
        page = await s._page_tab()
        frames = await self.tool("list_frames")
        child = next(frame["frame_id"] for frame in frames if frame["parent_id"])
        await self.tool("select_frame", frame_id=child)
        await self.navigate_frame(
            page,
            child,
            f"http://localhost:{self.http.server_port}/frame",
            separate=True,
        )
        result = await asyncio.wait_for(
            s.evaluate_script(
                "() => prompt('frame prompt')",
                prompt_text="from iframe",
                wait_for_stable=False,
            ),
            5,
        )
        self.assertIn("Status: completed", result)
        self.assertIn("from iframe", result)
        await s.enable_console_collection()
        await s.evaluate_script(
            "console.log('before-frame-dialog')", wait_for_stable=False
        )
        result = await asyncio.wait_for(
            s.evaluate_script(
                "() => confirm('frame manual')",
                dialog_action="manual",
                wait_for_stable=False,
            ),
            5,
        )
        self.assertIn("Status: interrupted", result)
        self.assertIn(
            "before-frame-dialog", await asyncio.wait_for(s.list_console_messages(), 2)
        )
        await s.handle_dialog(action="dismiss")
        self.assertFalse(s._dialog_state.current)

    async def test_bulk_fill_long_unicode_updates_controlled_models_in_one_insert(self):
        await s.new_page(self.url + "interaction")
        tab = await s._active_tab()
        snapshot = await s.take_snapshot()
        uid = re.search(r'uid=(\S+) textbox "Event input"', snapshot).group(1)
        textarea = re.search(r'uid=(\S+) textbox "Event textarea"', snapshot).group(1)
        value = "long 中文🙂" * 1000
        self.assertNotIn("Error", await s.fill(uid, value))
        self.assertEqual(
            await s._evaluate(
                tab, "[formValues['event-input'],controlledValues['event-input']]"
            ),
            [value, value],
        )
        events = await s._evaluate(tab, "window.inputEvents")
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["trusted"])
        history = await self.tool("get_connection_history", limit=25)
        inserts = [
            item for item in history if item["request"]["method"] == "Input.insertText"
        ]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0]["request"]["params"]["text"], value)
        await s._evaluate(tab, "window.keyEvents=[]")
        self.assertNotIn("Error", await s.fill(uid, "short"))
        keys = await s._evaluate(tab, "window.keyEvents")
        self.assertEqual(
            sum(event["type"] == "keypress" for event in keys), len("short")
        )
        lines = "line 中文😀\n" * 300
        self.assertNotIn("Error", await s.fill(textarea, lines))
        self.assertEqual(
            await s._evaluate(
                tab, "[formValues['event-textarea'],controlledValues['event-textarea']]"
            ),
            [lines, lines],
        )

    async def test_bulk_fill_contenteditable_and_constraints_preserve_controls(self):
        await s.new_page(self.url + "form-batch")
        tab = await s._active_tab()

        async def fill_id(element_id, value):
            uid = (await self.tool("find_elements", selector="#" + element_id))[0][
                "uid"
            ]
            return await s.fill(uid, value)

        value = "editable 中文🙂" * 100
        self.assertNotIn("Error", await fill_id("edit", value))
        self.assertEqual(await s._evaluate(tab, "window.editModel"), value)
        self.assertNotIn("Error", await fill_id("edit", ""))
        self.assertEqual(await s._evaluate(tab, "window.editModel"), "")
        for element_id in ("readonly", "disabled"):
            self.assertIn("Error", await fill_id(element_id, "changed"))
            self.assertEqual(
                await s._evaluate(
                    tab, f"document.getElementById('{element_id}').value"
                ),
                "keep",
            )
        self.assertIn("maxlength", await fill_id("limited", "🙂🙂"))
        self.assertEqual(
            await s._evaluate(tab, "document.getElementById('limited').value"), "old"
        )
        self.assertNotIn("Error", await fill_id("limited", "a🙂"))
        self.assertNotIn("Error", await fill_id("date", "2026-10-19"))
        self.assertEqual(
            await s._evaluate(tab, "document.getElementById('date').value"),
            "2026-10-19",
        )
        self.assertIn("not an editable", await fill_id("action", "ignored"))

    async def test_fill_form_stops_on_stale_uid_and_reports_completed_entries(self):
        await s.new_page(self.url + "form-batch")
        tab = await s._active_tab()
        first = (await self.tool("find_elements", selector="#first"))[0]["uid"]
        third = (await self.tool("find_elements", selector="#third"))[0]["uid"]
        with patch.object(s, "_discover_frames", wraps=s._discover_frames) as discover:
            result = await s.fill_form(
                [
                    {"uid": first, "value": "committed"},
                    {"uid": "stale-uid", "value": "bad"},
                    {"uid": third, "value": "must-not-run"},
                ],
                include_snapshot=True,
            )
        self.assertEqual(discover.await_count, 1)
        self.assertIn("Completed 1 of 3", result)
        self.assertIn("Skipped 1", result)
        self.assertIn("stale", result)
        self.assertEqual(
            await s._evaluate(tab, "[first.value,third.value]"), ["committed", "last"]
        )

    async def test_fill_form_stops_on_navigation_and_dialog_with_partial_progress(self):
        for mode in ("dialog", "navigate"):
            with self.subTest(mode=mode):
                await s.new_page(self.url + "form-batch")
                tab = await s._active_tab()
                await s._evaluate(tab, "window.batchMode=" + json.dumps(mode))
                uids = [
                    (await self.tool("find_elements", selector="#" + element_id))[0][
                        "uid"
                    ]
                    for element_id in ("first", "second", "third")
                ]
                result = await asyncio.wait_for(
                    s.fill_form(
                        [
                            {"uid": uid, "value": value}
                            for uid, value in zip(
                                uids, ("committed", "blocked", "must-not-run")
                            )
                        ],
                        include_snapshot=True,
                    ),
                    8,
                )
                self.assertIn("Completed 1 of 3", result)
                self.assertIn("Skipped 1", result)
                if mode == "dialog":
                    self.assertIn("batch interrupted", result)
                    await s.handle_dialog(action="dismiss")
                    self.assertEqual(await s._evaluate(tab, "third.value"), "last")
                else:
                    self.assertIn("Navigation", result)
                    await s.wait_for(["Destination ready"], timeout=5000)
                    self.assertNotIn(
                        "third",
                        await s._evaluate(
                            tab, "JSON.parse(sessionStorage.getItem('batchEvents'))"
                        ),
                    )

    async def test_extension_service_worker_action_targets_duplicate_urls(self):
        extension = Path(self.directory.name) / "action-extension"
        extension.mkdir()
        (extension / "manifest.json").write_text(
            json.dumps(
                {
                    "manifest_version": 3,
                    "name": "Action integration",
                    "version": "1.0",
                    "permissions": ["activeTab", "scripting"],
                    "host_permissions": ["http://127.0.0.1/*"],
                    "action": {"default_title": "Mark target page"},
                    "background": {"service_worker": "sw.js"},
                }
            )
        )
        (extension / "sw.js").write_text(
            "chrome.action.onClicked.addListener(tab => "
            "chrome.scripting.executeScript({target:{tabId:tab.id},func:()=>{"
            "document.body.dataset.extensionAction='clicked';}}));"
        )
        extension_id = await self.browser.send(
            uc.cdp.extensions.load_unpacked(str(extension))
        )
        try:
            first = await s._page_tab()
            await s.new_page(self.url)
            second = await s._page_tab()
            self.assertNotEqual(first.target_id, second.target_id)
            self.assertEqual(first.target.url, second.target.url)
            selected = s._selected_target_id
            for target, untouched in ((first, second), (second, None)):
                result = await self.tool(
                    "trigger_extension_action",
                    extension_id=extension_id,
                    page_id=str(target.target_id),
                )
                self.assertEqual(result, "Extension action triggered.")
                async with asyncio.timeout(5):
                    while (
                        await s._evaluate(
                            target, "document.body.dataset.extensionAction"
                        )
                        != "clicked"
                    ):
                        await asyncio.sleep(0.05)
                if untouched is not None:
                    self.assertIsNone(
                        await s._evaluate(
                            untouched, "document.body.dataset.extensionAction"
                        )
                    )
                self.assertEqual(s._selected_target_id, selected)
                self.assertIn("Main input", await s.take_snapshot())
            for target in (first, second):
                await s._evaluate(
                    target, "delete document.body.dataset.extensionAction"
                )
            await asyncio.gather(
                *(
                    self.tool(
                        "trigger_extension_action",
                        extension_id=extension_id,
                        page_id=str(target.target_id),
                    )
                    for target in (first, second)
                )
            )
            for target in (first, second):
                async with asyncio.timeout(5):
                    while (
                        await s._evaluate(
                            target, "document.body.dataset.extensionAction"
                        )
                        != "clicked"
                    ):
                        await asyncio.sleep(0.05)
            self.assertEqual(s._selected_target_id, selected)
        finally:
            await self.browser.send(uc.cdp.extensions.uninstall(extension_id))

    async def test_extension_service_worker_discovery_scripts_logs_and_retirement(self):
        extension = Path(self.directory.name) / "test-extension"
        extension.mkdir()
        (extension / "manifest.json").write_text(
            json.dumps(
                {
                    "manifest_version": 3,
                    "name": "Worker integration",
                    "version": "1.0",
                    "background": {"service_worker": "sw.js"},
                }
            )
        )
        (extension / "sw.js").write_text(
            "globalThis.workerValue='worker-ready';chrome.runtime.onInstalled.addListener(()=>console.log('worker-installed'));"
        )
        extension_id = await self.browser.send(
            uc.cdp.extensions.load_unpacked(str(extension))
        )
        selected = s._selected_target_id
        page = await s._page_tab()
        async with asyncio.timeout(10):
            while True:
                listed = await s.list_pages(include_service_workers=True)
                lines = [
                    line
                    for line in listed.splitlines()
                    if "service_worker_id" in line and extension_id in line
                ]
                if lines:
                    worker_id = json.loads(lines[0].strip())["service_worker_id"]
                    break
                await asyncio.sleep(0.05)
        self.assertEqual(s._selected_target_id, selected)
        with patch.object(
            s,
            "_perform_action",
            AsyncMock(side_effect=AssertionError("worker must not observe DOM")),
        ):
            result = await s.evaluate_script(
                "() => ({id:chrome.runtime.id,value:workerValue,document:typeof document})",
                service_worker_id=worker_id,
            )
        values = json.loads(result.splitlines()[1])
        self.assertEqual(
            values,
            {"id": extension_id, "value": "worker-ready", "document": "undefined"},
        )
        await s.enable_console_collection(service_worker_id=worker_id)
        await s.enable_console_collection(page_id=selected)
        await s.evaluate_script(
            "console.log('worker-isolation-marker');setTimeout(()=>{throw new Error('worker-async-error')},10)",
            format="script",
            service_worker_id=worker_id,
        )
        await s.evaluate_script(
            "console.log('page-isolation-marker')",
            wait_for_stable=False,
            page_id=selected,
        )
        async with asyncio.timeout(5):
            while "worker-async-error" not in await s.list_console_messages(
                service_worker_id=worker_id
            ):
                await asyncio.sleep(0.02)
        worker_logs = await s.list_console_messages(
            service_worker_id=worker_id, include_stack_traces=True
        )
        page_logs = await s.list_console_messages(page_id=selected)
        self.assertIn("worker-isolation-marker", worker_logs)
        self.assertNotIn("page-isolation-marker", worker_logs)
        self.assertNotIn("worker-isolation-marker", page_logs)
        msgid = int(
            re.search(r"\[(\d+)\] \[log\] worker-isolation-marker", worker_logs).group(
                1
            )
        )
        detail = await s.get_console_message(msgid, service_worker_id=worker_id)
        self.assertIn(extension_id, detail)
        self.assertIn(worker_id, detail)
        timeout = await s.evaluate_script(
            "new Promise(()=>{})", service_worker_id=worker_id, timeout=50
        )
        self.assertIn("Status: timed_out", timeout)
        self.assertIn(
            "worker-ready",
            await s.evaluate_script("workerValue", service_worker_id=worker_id),
        )
        source = Path(self.directory.name) / "worker-source.js"
        source.write_text("() => chrome.runtime.id")
        self.assertIn(
            extension_id,
            await s.evaluate_script(
                source_path=str(source), service_worker_id=worker_id
            ),
        )
        for options in ({"page_id": selected}, {"args": ["uid"]}):
            with self.assertRaises(ValueError):
                await s.evaluate_script("1", service_worker_id=worker_id, **options)
        with self.assertRaises(ValueError):
            await s.list_console_messages(page_id=selected, service_worker_id=worker_id)
        worker = s._worker_targets.connections[worker_id]
        await s.disable_console_collection(service_worker_id=worker_id)
        self.assertFalse(worker.handlers.get(uc.cdp.runtime.ConsoleAPICalled))
        self.assertTrue(page.handlers.get(uc.cdp.runtime.ConsoleAPICalled))
        await s.enable_console_collection(service_worker_id=worker_id)
        await self.browser.send(uc.cdp.extensions.uninstall(extension_id))
        async with asyncio.timeout(5):
            while extension_id in await s.list_pages(include_service_workers=True):
                await asyncio.sleep(0.05)
        self.assertFalse(worker.handlers.get(uc.cdp.runtime.ConsoleAPICalled))
        self.assertIn(
            "Unknown or stopped",
            await s.evaluate_script("1", service_worker_id=worker_id),
        )
        self.assertEqual(s._selected_target_id, selected)

    async def test_cross_process_source_map_id_reuse_preserves_old_log_ownership(self):
        await s.enable_console_collection(source_maps=True)
        logs = []
        for host, label in (("source-a.localhost", "A"), ("source-b.localhost", "B")):
            url = f"http://{host}:{self.http.server_port}/identity-page?name={label}"
            await s.navigate_page(url=url)
            async with asyncio.timeout(5):
                while not any(
                    m["text"] == f"identity-{label}" for m in s._console_messages
                ):
                    await asyncio.sleep(0.01)
            message = next(
                m for m in s._console_messages if m["text"] == f"identity-{label}"
            )
            logs.append(message)
            detail = await s.get_console_message(message["msgid"])
            self.assertIn(f"/{label}.ts:1:1", detail)
            self.assertIn(f"source-{label}", detail)
        self.assertEqual(
            logs[0]["location"]["script_id"], logs[1]["location"]["script_id"]
        )
        old = await s.get_console_message(logs[0]["msgid"])
        self.assertIn("/A.ts:1:1", old)
        self.assertIn("source-A", old)
        self.assertNotIn("/B.ts", old)

    async def test_cross_process_source_maps_map_logs_emitted_before_collection(self):
        tab = await s._page_tab()
        origin = f"http://source-a.localhost:{self.http.server_port}"
        await s.navigate_page(url=origin + "/identity-page?name=Replay")
        await s.evaluate_script(
            "new Promise(resolve=>{const script=document.createElement('script');"
            "script.src='/identity-script?name=Pending';script.onload=()=>resolve(true);"
            "document.head.append(script);})",
            wait_for_stable=False,
        )
        self.assertNotIn(s._tab_key(tab), s._console_collection_enabled_tabs)
        self.assertFalse(tab.handlers.get(uc.cdp.debugger.ScriptParsed))
        await s.enable_console_collection(source_maps=True)
        async with asyncio.timeout(5):
            while not any(m["text"] == "identity-Replay" for m in s._console_messages):
                await asyncio.sleep(0.01)
        replay = next(m for m in s._console_messages if m["text"] == "identity-Replay")
        detail = await s.get_console_message(replay["msgid"])
        self.assertIn("/Replay.ts:1:1", detail)
        self.assertIn("source-Replay", detail)
        # Leave this second map uncached until Debugger has been re-enabled.
        pending = next(
            m for m in s._console_messages if m["text"] == "identity-Pending"
        )
        await s.enable_console_collection(source_maps=False)
        await s.enable_console_collection(source_maps=True)
        detail = await s.get_console_message(pending["msgid"])
        self.assertIn("/Pending.ts:1:1", detail)
        self.assertIn("source-Pending", detail)
        listed = await s.list_console_messages(include_stack_traces=True)
        self.assertIn("/Replay.ts:1:1", listed)

        other = f"http://source-b.localhost:{self.http.server_port}"
        await s.navigate_page(url=other + "/identity-page?name=After")
        async with asyncio.timeout(5):
            while not any(m["text"] == "identity-After" for m in s._console_messages):
                await asyncio.sleep(0.01)
        current = next(m for m in s._console_messages if m["text"] == "identity-After")
        self.assertEqual(
            replay["location"]["script_id"], current["location"]["script_id"]
        )
        detail = await s.get_console_message(current["msgid"])
        self.assertIn("/After.ts:1:1", detail)
        self.assertIn("source-After", detail)
        old = await s.get_console_message(replay["msgid"])
        self.assertIn("/Replay.ts:1:1", old)
        self.assertNotIn("/After.ts", old)

    async def test_source_map_initialization_cancellation_retries_existing_scripts(
        self,
    ):
        tab = await s._page_tab()
        for phase in ("script_replay", "enable_response"):
            with self.subTest(phase=phase):
                await s.disable_console_collection()
                await s.navigate_page(url=self.url + "identity-page?name=Cancelled")
                script_url = self.url + "identity-script?name=Cancelled"
                entered, release = asyncio.Event(), asyncio.Event()
                original_send, original_event = tab.send, tab.process_event
                paused = False
                commands = []

                async def send(command, **kwargs):
                    nonlocal paused
                    enabling = command.gi_code is uc.cdp.debugger.enable.__code__
                    if enabling:
                        commands.append("enable")
                    elif command.gi_code is uc.cdp.debugger.disable.__code__:
                        commands.append("disable")
                    result = await original_send(command, **kwargs)
                    if enabling and phase == "enable_response" and not paused:
                        paused = True
                        entered.set()
                        await release.wait()
                    return result

                async def process_event(message, tx_id=None):
                    nonlocal paused
                    await original_event(message, tx_id)
                    if (
                        phase == "script_replay"
                        and not paused
                        and message.get("method") == "Debugger.scriptParsed"
                        and message.get("params", {}).get("url") == script_url
                    ):
                        paused = True
                        entered.set()
                        await release.wait()

                with (
                    patch.object(tab, "send", send),
                    patch.object(tab, "process_event", process_event),
                ):
                    initializing = asyncio.create_task(
                        s.enable_console_collection(source_maps=True)
                    )
                    try:
                        await asyncio.wait_for(entered.wait(), 5)
                        initializing.cancel()
                        release.set()
                        with self.assertRaises(asyncio.CancelledError):
                            await initializing
                        self.assertNotIn(
                            s._tab_key(tab), s._console_collection_enabled_tabs
                        )
                        self.assertNotIn(s._tab_key(tab), s._console_callbacks)
                        self.assertFalse(tab.handlers.get(uc.cdp.debugger.ScriptParsed))
                        await s.enable_console_collection(source_maps=True)
                        previous = {m["msgid"] for m in s._console_messages}
                        await s.evaluate_script("minified()", wait_for_stable=False)
                        async with asyncio.timeout(5):
                            while not any(
                                m["text"] == "identity-Cancelled"
                                and m["msgid"] not in previous
                                for m in s._console_messages
                            ):
                                await asyncio.sleep(0.01)
                        message = next(
                            m
                            for m in s._console_messages
                            if m["text"] == "identity-Cancelled"
                            and m["msgid"] not in previous
                        )
                        detail = await s.get_console_message(message["msgid"])
                        self.assertIn("/Cancelled.ts:1:1", detail)
                        self.assertIn("source-Cancelled", detail)
                        maps = s._console_callbacks[s._tab_key(tab)].source_maps
                        self.assertTrue(
                            any(
                                script["url"] == script_url
                                for script in maps.scripts.values()
                            )
                        )
                        self.assertEqual(commands, ["enable", "disable", "enable"])
                        self.assertFalse(tab._mapper)
                    finally:
                        release.set()
                        if not initializing.done():
                            initializing.cancel()
                        await asyncio.gather(initializing, return_exceptions=True)
                        await s.disable_console_collection()

    async def test_source_maps_use_browser_credentials_and_symbolize_async_stacks(self):
        tab = await s._active_tab()
        self.assertFalse(tab.handlers.get(uc.cdp.debugger.ScriptParsed))
        await s.set_cookie("map_access", "yes", "127.0.0.1")
        await s.enable_console_collection(source_maps=True)
        await s.evaluate_script(
            "() => new Promise((resolve,reject)=>{const script=document.createElement('script');script.src='/mapped.js';script.onload=resolve;script.onerror=reject;document.head.append(script);})",
            wait_for_stable=False,
        )
        await s.evaluate_script("deployedCrash()", wait_for_stable=False)
        async with asyncio.timeout(5):
            while not any(
                m["source"] == "exception" and "mapped-exception-marker" in m["text"]
                for m in s._console_messages
            ):
                await asyncio.sleep(0.02)
        records = [
            m
            for m in s._console_messages
            if "mapped-" in m["text"] and m["connection"] is tab
        ]
        self.assertEqual(len(records), 2)
        for message in records:
            detail = await s.get_console_message(message["msgid"])
            self.assertIn("/src/original.ts:5:3", detail)
            self.assertIn("originalLogger", detail)
            self.assertIn("generated:", detail)
            self.assertIn("mapped.js", detail)
            native = await s.get_console_message(
                message["msgid"], resolve_source_maps=False
            )
            self.assertNotIn("originalLogger", native)
        listed = await s.list_console_messages(include_stack_traces=True)
        self.assertIn("originalLogger", listed)
        self.assertTrue(
            any(
                "map_access=yes" in h.get("Cookie", "")
                for h in self.http.source_map_headers
            )
        )
        await s.enable_console_collection(source_maps=False)
        self.assertFalse(tab.handlers.get(uc.cdp.debugger.ScriptParsed))
        await s.disable_console_collection()
        self.assertFalse(tab.handlers.get(uc.cdp.runtime.ConsoleAPICalled))
        self.assertFalse(tab.handlers.get(uc.cdp.debugger.ScriptFailedToParse))

    async def test_script_files_support_formats_elements_and_output_paths(self):
        source = Path(self.directory.name) / "源 文件.js"
        source.write_text('() => "文件🙂"', encoding="utf-8-sig")
        self.assertIn(
            "文件🙂",
            await s.evaluate_script(source_path=str(source), wait_for_stable=False),
        )
        source.write_text(
            "globalThis.fileRuns=(globalThis.fileRuns||0)+1;fileRuns", encoding="utf-8"
        )
        result = await s.evaluate_script(
            source_path=source.as_uri(), format="script", wait_for_stable=False
        )
        self.assertEqual(json.loads(result.splitlines()[1]), 1)
        uid = re.search(
            r'uid=(\S+) textbox "Main input"', await s.take_snapshot()
        ).group(1)
        source.write_text("(el) => el.tagName", encoding="utf-8")
        self.assertIn(
            "INPUT",
            await s.evaluate_script(
                source_path=str(source),
                args=[uid],
                format="function",
                wait_for_stable=False,
            ),
        )
        output = Path(self.directory.name) / "输出.json"
        await s.evaluate_script(
            source_path=str(source),
            args=[uid],
            file_path=str(output),
            wait_for_stable=False,
        )
        self.assertEqual(json.loads(output.read_text()), "INPUT")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            await s.evaluate_script("1", source_path=str(source))

    async def test_script_formats_execute_functions_iifes_and_statements_once(self):
        await s.evaluate_script(
            "window.value=9", format="script", wait_for_stable=False
        )
        self.assertEqual(
            json.loads(
                (await s.evaluate_script("value", wait_for_stable=False)).splitlines()[
                    1
                ]
            ),
            9,
        )
        result = await s.evaluate_script(
            "(() => {window.executionCount=(window.executionCount||0)+1;return {count:window.executionCount};})()",
            wait_for_stable=False,
        )
        self.assertEqual(json.loads(result.splitlines()[1]), {"count": 1})
        result = await s.evaluate_script(
            "(window.executionCount++,42)", wait_for_stable=False
        )
        self.assertEqual(json.loads(result.splitlines()[1]), 42)
        self.assertEqual(
            json.loads(
                (
                    await s.evaluate_script(
                        "window.executionCount", wait_for_stable=False
                    )
                ).splitlines()[1]
            ),
            2,
        )
        for source, mode in (
            ("() => 42", "function"),
            ("var statementResult=6*7; statementResult", "script"),
            ("var autoResult=21*2; autoResult", "auto"),
        ):
            result = await s.evaluate_script(source, format=mode, wait_for_stable=False)
            self.assertIn("Status: completed", result)
            self.assertEqual(json.loads(result.splitlines()[1]), 42)
        result = await s.evaluate_script("123n", wait_for_stable=False)
        self.assertIn("Status: failed", result)
        self.assertIn("bigint", result)
        result = await s.evaluate_script(
            "(() => {window.executionCount++;throw new SyntaxError('runtime syntax marker');})()",
            wait_for_stable=False,
        )
        self.assertIn("Status: failed", result)
        self.assertEqual(
            json.loads(
                (
                    await s.evaluate_script(
                        "window.executionCount", wait_for_stable=False
                    )
                ).splitlines()[1]
            ),
            3,
        )
        await s.evaluate_script(
            "window.Function=() => {throw new Error('patched global Function');}",
            format="script",
            wait_for_stable=False,
        )
        self.assertEqual(
            json.loads(
                (
                    await s.evaluate_script("() => 42", wait_for_stable=False)
                ).splitlines()[1]
            ),
            42,
        )
        await s.new_page(self.url + "strict-csp")
        self.assertEqual(
            json.loads(
                (
                    await s.evaluate_script("() => 42", wait_for_stable=False)
                ).splitlines()[1]
            ),
            42,
        )
        self.assertNotIn(
            s._tab_key(await s._page_tab()), s._console_collection_enabled_tabs
        )

    async def test_script_native_deadline_stops_busy_execution_and_later_calls_work(
        self,
    ):
        page = await s._page_tab()
        result = await asyncio.wait_for(
            s.evaluate_script(
                "() => {while(true){}}", timeout=100, wait_for_stable=False
            ),
            5,
        )
        self.assertIn("Status: timed_out", result)
        self.assertEqual(
            json.loads(
                (await s.evaluate_script("42", wait_for_stable=False)).splitlines()[1]
            ),
            42,
        )
        snapshot = await s.take_snapshot()
        uid = re.search(r'uid=(\S+) textbox "Main input"', snapshot).group(1)
        result = await asyncio.wait_for(
            s.evaluate_script(
                "async el => {await new Promise(() => {});return el.value;}",
                [uid],
                timeout=100,
                wait_for_stable=False,
            ),
            5,
        )
        self.assertIn("Status: timed_out", result)
        self.assertEqual(
            json.loads(
                (await s.evaluate_script("42", wait_for_stable=False)).splitlines()[1]
            ),
            42,
        )
        self.assertFalse(page._mapper)

    async def test_argument_script_busy_deadline_restores_page_and_keeps_promise_waits_safe(
        self,
    ):
        page = await s._page_tab()
        uid = re.search(
            r'uid=(\S+) textbox "Main input"', await s.take_snapshot()
        ).group(1)
        for source in (
            "el => {while(true){}}",
            "async el => {await new Promise(r=>setTimeout(r,10));while(true){}}",
            "el => new Promise(() => {})",
        ):
            with self.subTest(source=source):
                recovered = False
                try:
                    result = await asyncio.wait_for(
                        s.evaluate_script(
                            source, [uid], timeout=200, wait_for_stable=False
                        ),
                        5,
                    )
                    self.assertIn("Status: timed_out", result)
                    next_result = await asyncio.wait_for(
                        s.evaluate_script("42", timeout=1000, wait_for_stable=False), 3
                    )
                    self.assertIn("Status: completed", next_result)
                    self.assertEqual(json.loads(next_result.splitlines()[1]), 42)
                    recovered = True
                finally:
                    # A failing implementation must not leave teardown stuck in JS.
                    if not recovered:
                        await asyncio.wait_for(
                            page.send(uc.cdp.runtime.terminate_execution()), 2
                        )
        self.assertNotIn(s._tab_key(page), s._console_collection_enabled_tabs)
        self.assertFalse(page._mapper)

        opened = await s.new_page(self.url, background=True)
        other = re.search(r"page_id=(\w+)", opened).group(1)
        timed_out, completed = await asyncio.gather(
            s.evaluate_script(
                "el => new Promise(() => {})",
                [uid],
                timeout=100,
                wait_for_stable=False,
                page_id=str(page.target_id),
            ),
            s.evaluate_script(
                "new Promise(resolve=>setTimeout(()=>resolve(84),300))",
                timeout=1500,
                wait_for_stable=False,
                page_id=other,
            ),
        )
        self.assertIn("Status: timed_out", timed_out)
        self.assertIn("Status: completed", completed)
        self.assertEqual(json.loads(completed.splitlines()[1]), 84)

    async def test_script_large_results_save_complete_json_and_keep_bounded_previews(
        self,
    ):
        result = await s.evaluate_script(
            "() => ({rows:Array.from({length:500}, (_,i)=>({i,text:'中文🙂'.repeat(10)}))})",
            max_output_chars=200,
            wait_for_stable=False,
        )
        path = Path(json.loads(result.splitlines()[0].partition(": ")[2]))
        value = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(value["rows"]), 500)
        self.assertEqual(value["rows"][-1]["i"], 499)
        self.assertIn("Preview truncated", result)
        self.assertLess(len(result), 1500)
        explicit = Path(self.directory.name) / "requested" / "result 中文.json"
        result = await s.evaluate_script(
            "() => ({ok:false,n:0})", file_path=str(explicit), wait_for_stable=False
        )
        self.assertEqual(
            json.loads(explicit.read_text(encoding="utf-8")), {"ok": False, "n": 0}
        )
        await s.close_browser()
        self.assertTrue(path.exists())
        self.assertTrue(explicit.exists())

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
            self.assertEqual(
                len(connection.handlers[uc.cdp.runtime.ExceptionThrown]), 1
            )
            self.assertEqual(
                len(connection.handlers[uc.cdp.runtime.ExceptionRevoked]), 1
            )
            await s._evaluate(
                connection, "setTimeout(() => {throw new Error('iframe-uncaught')}, 0)"
            )
            async with asyncio.timeout(5):
                while not any(
                    "iframe-uncaught" in m["text"] and m["connection"] is connection
                    for m in s._console_messages
                ):
                    await asyncio.sleep(0.01)
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
        self.assertFalse(old.handlers.get(uc.cdp.runtime.ExceptionThrown))
        self.assertFalse(old.handlers.get(uc.cdp.runtime.ExceptionRevoked))
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

    async def test_screenshot_limits_cover_viewport_full_page_and_scrolled_element(
        self,
    ):
        await s.emulate(viewport="800x600x2")
        await s.evaluate_script("""() => { document.body.innerHTML='<button style="position:absolute;left:20px;top:1100px;width:400px;height:200px;border:0;padding:0;background:rgb(17,34,51)">Crop target</button>';
            document.body.style.margin='0'; document.body.style.height='2400px';return true; }""")
        uid = re.search(
            r'uid=(\S+) button "Crop target"', await s.take_snapshot()
        ).group(1)
        for format in ("png", "jpeg", "webp"):
            for full in (False, True):
                original = PillowImage.open(
                    io.BytesIO(
                        (await s.take_screenshot(full_page=full, format=format)).data
                    )
                )
                shot = await s.take_screenshot(
                    full_page=full, format=format, max_width=320, max_height=240
                )
                image = PillowImage.open(io.BytesIO(shot.data))
                self.assertLessEqual(image.width, 320)
                self.assertLessEqual(image.height, 240)
                self.assertAlmostEqual(
                    image.width / image.height,
                    original.width / original.height,
                    delta=0.02,
                )
                self.assertEqual(image.format.lower(), format)
            await s.evaluate_script("scrollTo(0,1000)", wait_for_stable=False)
            crop = PillowImage.open(
                io.BytesIO(
                    (
                        await s.take_screenshot(
                            uid=uid, format=format, max_width=240, max_height=100
                        )
                    ).data
                )
            ).convert("RGB")
            self.assertEqual(crop.size, (200, 100))
            self.assertTrue(
                all(abs(a - b) < 5 for a, b in zip(crop.getpixel((5, 5)), (17, 34, 51)))
            )
        saved = Path(self.directory.name) / "limited.png"
        await s.take_screenshot(file_path=str(saved), max_width=300)
        with PillowImage.open(saved) as saved_image:
            self.assertLessEqual(saved_image.width, 300)
        small = PillowImage.open(
            io.BytesIO((await s.take_screenshot(uid=uid, max_width=5000)).data)
        )
        self.assertEqual(small.size, (800, 400))
        with self.assertRaisesRegex(ValueError, "positive"):
            await s.take_screenshot(max_height=0)

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

    async def test_network_redirect_extra_headers_and_body_exports(self):
        tab = await s._active_tab()
        await s.evaluate_script(
            "() => fetch('/net-redirect').then(r => r.text())", wait_for_stable=False
        )
        async with asyncio.timeout(5):
            while True:
                records = [
                    r
                    for r in s._network_requests
                    if r["connection"] is tab and "/net-" in r["url"]
                ]
                if (
                    len(records) == 3
                    and records[-1].get("finished")
                    and all(r.get("response_extra_info") for r in records)
                ):
                    break
                await asyncio.sleep(0.02)
        first, second, final = records
        self.assertEqual([r["status"] for r in records], [302, 302, 200])
        self.assertEqual(
            [r["reqid"] for r in final["redirect_chain"]],
            [first["reqid"], second["reqid"]],
        )
        self.assertIn("hop=first", json.dumps(second["headers"]))
        self.assertIn("hop=second", json.dumps(final["headers"]))
        self.assertIn("Set-Cookie", json.dumps(first["response_headers"]))
        self.assertIn(
            '"response_body_state": "redirect"',
            await s.get_network_request(first["reqid"]),
        )
        self.assertIn("redirect-complete", await s.get_network_request(final["reqid"]))
        payload = "请求正文🙂" * 1000
        await s.evaluate_script(
            "() => fetch('/net-post',{method:'POST',body:"
            + json.dumps(payload)
            + "}).then(r=>r.text())",
            wait_for_stable=False,
            max_output_chars=20,
        )
        async with asyncio.timeout(5):
            while True:
                posted = next(
                    (
                        r
                        for r in reversed(s._network_requests)
                        if "/net-post" in r["url"]
                    ),
                    None,
                )
                if posted and posted.get("finished"):
                    break
                await asyncio.sleep(0.02)
        detail = await s.get_network_request(posted["reqid"], max_body_chars=40)
        self.assertNotIn(payload, detail)
        self.assertIn("preview truncated", detail)
        post, result = (
            Path(self.directory.name) / "post.txt",
            Path(self.directory.name) / "response.txt",
        )
        await s.get_network_request(
            posted["reqid"], str(post), str(result), max_body_chars=10
        )
        self.assertEqual(post.read_text(), payload)
        self.assertEqual(result.read_text(), payload)
        self.assertIn(
            "not_requested",
            await s.get_network_request(posted["reqid"], include_body=False),
        )

    @unittest.skipUnless(
        os.environ.get("NODRIVER_MCP_PERFORMANCE_TESTS") == "1",
        "Native DevTools engine checks are opt-in",
    )
    async def test_real_trace_engine_reports_vitals_and_native_insight_details(self):
        await s.performance_start_trace(reload=False, auto_stop=False)
        await s.navigate_page(url=self.url + "performance")
        await asyncio.sleep(0.5)
        snapshot = await s.take_snapshot()
        button = re.search(r'uid=(\S+) button "Trace action"', snapshot).group(1)
        await s.click(button)
        await asyncio.sleep(0.15)
        report = await s.performance_stop_trace(analyze=True)
        self.assertIn("Engine: chrome-devtools-mcp@1.11.0", report)
        self.assertFalse(s._tracing_active)
        self.assertFalse(s._trace_stopping)
        analysis_path = Path(
            json.loads(
                re.search(r"^Analysis file: (.+)$", report, re.MULTILINE).group(1)
            )
        )
        data = json.loads(analysis_path.read_text())
        own = next(
            v for v in data["web_vitals"] if v["url"] == self.url + "performance"
        )
        self.assertGreater(own["lcp_ms"], 0)
        self.assertGreater(own["inp_ms"], 0)
        self.assertIsNotNone(own["cls"])
        self.assertGreaterEqual(own["cls"], 0)
        self.assertEqual(data["insight_sets"][0]["errors"], {})
        self.assertTrue(Path(data["trace_path"]).is_file())
        detailed = await s.performance_analyze_insight(
            own["insight_set_id"], "INPBreakdown", max_output_chars=10000
        )
        self.assertIn("Input delay", detailed)
        self.assertIn("Processing duration", detailed)
        automatic = await s.performance_start_trace(
            reload=True, duration_ms=600, analyze=True
        )
        self.assertIn("Engine: chrome-devtools-mcp@1.11.0", automatic)
        self.assertFalse(s._tracing_active)
        await s.close_browser()
        offline = await s.performance_analyze_trace(max_output_chars=100)
        self.assertIn("preview truncated", offline)
        self.assertIsNone(s._browser)
        self.assertTrue(analysis_path.exists())

    async def test_trace_manual_stop_owns_state_when_auto_recording_is_cancelled(self):
        started, reading, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        page = await s._page_tab()
        original_send, original_read = page.send, s._read_stream

        async def send(command, **kwargs):
            start = command.gi_code is uc.cdp.tracing.start.__code__
            result = await original_send(command, **kwargs)
            if start:
                started.set()
            return result

        async def read(*args):
            data = await original_read(*args)
            reading.set()
            await release.wait()
            return data

        with patch.object(page, "send", send), patch.object(s, "_read_stream", read):
            automatic = asyncio.create_task(
                s.performance_start_trace(reload=False, duration_ms=60000)
            )
            stopping = None
            try:
                await asyncio.wait_for(started.wait(), 5)
                state = s._trace_metadata
                stopping = asyncio.create_task(s.performance_stop_trace(analyze=False))
                await asyncio.wait_for(reading.wait(), 10)
                automatic.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await automatic
                self.assertTrue(s._tracing_active)
                self.assertTrue(s._trace_stopping)
                self.assertIs(s._trace_metadata, state)
                self.assertIn(
                    "already running",
                    await s.performance_start_trace(reload=False, auto_stop=False),
                )
                release.set()
                self.assertIn("Trace stopped", await stopping)
            finally:
                release.set()
                automatic.cancel()
                await asyncio.gather(
                    automatic, *([stopping] if stopping else []), return_exceptions=True
                )
        self.assertIn(
            "Trace started",
            await s.performance_start_trace(reload=False, auto_stop=False),
        )
        self.assertTrue(s._tracing_active)
        self.assertIn("Trace stopped", await s.performance_stop_trace(analyze=False))

    async def test_trace_auto_and_manual_exports_keep_complete_files_and_config(self):
        first = str((await s._page_tab()).target_id)
        await s.performance_start_trace(reload=False, auto_stop=False, page_id=first)
        await s.evaluate_script(
            "performance.mark('complete-file-marker')",
            wait_for_stable=False,
            page_id=first,
        )
        await s.new_page(self.url, background=False)
        report = await s.performance_stop_trace()
        info = json.loads(report.partition("\n")[2])
        path = Path(info["file_path"])
        self.assertTrue(path.is_absolute())
        self.assertEqual(info["config"]["page_id"], first)
        events = json.loads(path.read_text())["traceEvents"]
        self.assertEqual(info["event_count"], len(events))
        self.assertTrue(any(e["name"] == "complete-file-marker" for e in events))
        self.assertGreater(info["duration_ms"], 0)
        gzip_path = Path(self.directory.name) / "trace.json.gz"
        report = await s.performance_start_trace(
            reload=True, duration_ms=250, file_path=str(gzip_path), ignore_cache=True
        )
        info = json.loads(report.partition("\n")[2])
        import gzip

        self.assertTrue(
            json.loads(gzip.decompress(gzip_path.read_bytes()))["traceEvents"]
        )
        self.assertTrue(info["config"]["ignore_cache"])
        self.assertFalse(s._tracing_active)
        self.assertFalse(s._trace_stopping)
        tab = await s._active_tab()
        self.assertFalse(tab.handlers.get(uc.cdp.tracing.TracingComplete))
        await s.close_browser()
        self.assertTrue(path.exists())
        self.assertTrue(gzip_path.exists())

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

    async def test_same_process_frame_scripts_use_main_world_and_preserve_logs(self):
        page = await s._page_tab()

        async def load_frame(value):
            html = (
                '<meta http-equiv="Content-Security-Policy" content="script-src \'nonce-fixture\'">'
                '<script nonce="fixture">'
                f"let frameLexical={value};window.frameApp={{answer:{value}}};"
                "function frameAnswer(){return frameLexical};"
                'console.log("before-frame-world");</script>'
                '<input aria-label="World field">'
            )
            await page.send(
                uc.cdp.runtime.evaluate(
                    "new Promise(resolve=>{const frame=document.querySelector('iframe');"
                    "frame.onload=()=>resolve(true);frame.srcdoc="
                    + json.dumps(html)
                    + ";})",
                    await_promise=True,
                )
            )

        await load_frame(42)
        frames = await self.tool("list_frames")
        frame = next(frame for frame in frames if frame["name"] == "child")
        self.assertFalse(frame["separate_connection"])
        await self.tool("select_frame", frame_id=frame["frame_id"])
        for value in (42, 84):
            if value == 84:
                await load_frame(value)
            result = await s.evaluate_script(
                "() => [window.frameApp.answer,frameLexical,frameAnswer()]",
                wait_for_stable=False,
            )
            self.assertIn("Status: completed", result)
            self.assertEqual(json.loads(result.splitlines()[1]), [value] * 3)
            self.assertEqual(
                await s._evaluate(await s._active_tab(), "frameAnswer()"), value
            )
        await s.evaluate_script(
            "var frameCreated=21", format="script", wait_for_stable=False
        )
        await s.evaluate_script(
            "let frameCreatedLet=21;const frameCreatedConst=42;",
            format="script",
            wait_for_stable=False,
        )
        self.assertIn(
            "63",
            await s.evaluate_script(
                "frameCreatedLet+frameCreatedConst", wait_for_stable=False
            ),
        )
        self.assertIn(
            "42", await s.evaluate_script("frameCreated*2", wait_for_stable=False)
        )
        await s.enable_console_collection(source_maps=True)
        self.assertTrue(
            any(m["text"] == "before-frame-world" for m in s._console_messages)
        )
        await s.disable_console_collection()
        self.assertIn(
            "84", await s.evaluate_script("frameAnswer()", wait_for_stable=False)
        )

    async def test_same_process_frame_actions_never_enable_runtime_or_leave_bindings(
        self,
    ):
        page = await s._page_tab()
        methods = []
        native_send = page.send

        async def send(command, *args, **kwargs):
            packet = next(command)
            methods.append(packet["method"])

            def replay():
                response = yield packet
                try:
                    command.send(response)
                except StopIteration as stop:
                    return stop.value

            return await native_send(replay(), *args, **kwargs)

        with patch.object(page, "send", send):
            await page.send(
                uc.cdp.runtime.evaluate("""(() => {
                window.runtimeEnableLeakDetected=false;
                const error=new Error('runtime-enable-probe');
                Object.defineProperty(error,'name',{get(){window.runtimeEnableLeakDetected=true;return 'Error'}});
                console.log(error);
                window.runtimeEnableLeakDetected=false;
                document.querySelector('iframe').contentDocument.body.insertAdjacentHTML('beforeend','<p>iframe marker</p>');
            })()""")
            )
            self.assertFalse(
                (
                    await page.send(
                        uc.cdp.runtime.evaluate(
                            "runtimeEnableLeakDetected", return_by_value=True
                        )
                    )
                )[0].value
            )
            child = next(
                frame
                for frame in await self.tool("list_frames")
                if frame["name"] == "child"
            )
            await self.tool("select_frame", frame_id=child["frame_id"])
            first = await s.evaluate_script(
                "let frameLexicalProbe=21;const frameConstProbe=42;var frameVarProbe=7;",
                format="script",
                wait_for_stable=False,
            )
            self.assertIn("Status: completed", first)
            value = await s.evaluate_script(
                "[frameLexicalProbe,frameConstProbe,frameVarProbe]",
                wait_for_stable=False,
            )
            self.assertEqual(json.loads(value.splitlines()[1]), [21, 42, 7])
            field = (await self.tool("find_elements", selector="input"))[0]["uid"]
            self.assertNotIn("Error", await s.click(field))
            self.assertNotIn("Error", await s.fill(field, "without-runtime"))
            self.assertIn(
                "Found text", await s.wait_for(["iframe marker"], timeout=2000)
            )
            timed = await s.evaluate_script(
                "while(true){}", format="script", timeout=100, wait_for_stable=False
            )
            self.assertIn("Status: timed_out", timed)
            self.assertIn(
                "Status: completed",
                await s.evaluate_script("42", wait_for_stable=False),
            )
            remaining = (
                await page.send(
                    uc.cdp.runtime.evaluate(
                        "[window,document.querySelector('iframe').contentWindow].map(w=>Object.getOwnPropertyNames(w).filter(n=>n.startsWith('__mcp_context_')))",
                        return_by_value=True,
                    )
                )
            )[0].value
            self.assertEqual(remaining, [[], []])
            self.assertFalse(page.handlers.get(uc.cdp.runtime.BindingCalled))
            self.assertNotIn("Runtime.enable", methods)
            self.assertNotIn("Runtime.disable", methods)
            self.assertNotIn("Debugger.enable", methods)
            self.assertFalse(
                (
                    await page.send(
                        uc.cdp.runtime.evaluate(
                            "runtimeEnableLeakDetected", return_by_value=True
                        )
                    )
                )[0].value
            )
            # Positive control proves that the buffered console probe can see
            # the exact signal that frame discovery must avoid.
            await page.send(uc.cdp.runtime.enable())
            self.assertTrue(
                (
                    await page.send(
                        uc.cdp.runtime.evaluate(
                            "runtimeEnableLeakDetected", return_by_value=True
                        )
                    )
                )[0].value
            )

    async def test_same_process_iframe_xpath_wait_keeps_nodes_inserted_during_query(
        self,
    ):
        from nodriver_mcp import advanced

        page = await s._page_tab()
        await page.send(
            uc.cdp.runtime.evaluate(
                "document.querySelector('iframe').contentDocument.body.innerHTML=''"
            )
        )
        child = next(
            frame
            for frame in await self.tool("list_frames")
            if frame["name"] == "child"
        )
        await self.tool("select_frame", frame_id=child["frame_id"])
        native_xpath = advanced._frame_xpath

        async def delayed_node(*args):
            await page.send(
                uc.cdp.runtime.evaluate(
                    "setTimeout(()=>{const doc=document.querySelector('iframe').contentDocument;"
                    "const input=doc.createElement('input');input.id='late-xpath';"
                    "doc.body.append(input)},300)"
                )
            )
            return await native_xpath(*args)

        with patch.object(advanced, "_frame_xpath", delayed_node):
            matches = await self.tool(
                "find_elements", xpath="//input[@id='late-xpath']", timeout=2
            )
        self.assertEqual(len(matches), 1)
        self.assertNotIn("Error", await s.fill(matches[0]["uid"], "dynamic-node"))
        self.assertEqual(
            (
                await page.send(
                    uc.cdp.runtime.evaluate(
                        "document.querySelector('iframe').contentDocument.querySelector('input').value",
                        return_by_value=True,
                    )
                )
            )[0].value,
            "dynamic-node",
        )

    async def test_cross_process_nested_frames_route_scripts_and_elements(self):
        page = await s._page_tab()
        outer_url = f"http://localhost:{self.http.server_port}/nested-outer"
        await page.send(
            uc.cdp.runtime.evaluate(
                "new Promise(resolve=>{const frame=document.querySelector('iframe');"
                "frame.onload=()=>resolve(true);frame.src="
                + json.dumps(outer_url)
                + ";})",
                await_promise=True,
            )
        )
        frames = await self.tool("list_frames")
        outer = next(frame for frame in frames if frame["url"] == outer_url)
        inner = next(
            frame for frame in frames if frame["url"].endswith("/nested-inner")
        )
        self.assertTrue(inner["separate_connection"])
        self.assertEqual(inner["parent_id"], outer["frame_id"])
        self.assertIn("Nested inner marker", await s.take_snapshot(include_frames=True))
        await self.tool("select_frame", frame_id=inner["frame_id"])
        connection = await s._active_tab()
        self.assertEqual(str(connection.target_id), outer["frame_id"])
        result = await s.evaluate_script(
            "() => [frameLevel,frameLexical]", wait_for_stable=False
        )
        self.assertIn("Status: completed", result)
        self.assertEqual(json.loads(result.splitlines()[1]), ["inner", 42])
        self.assertEqual(await s._evaluate(connection, "frameLevel"), "inner")
        self.assertIn("Nested inner marker", await s.take_snapshot())
        for query in (
            {"selector": "input"},
            {"text": "Nested inner marker"},
            {"xpath": "//input", "timeout": 0},
        ):
            with self.subTest(query=query):
                self.assertTrue(await self.tool("find_elements", **query))
        field = (await self.tool("find_elements", selector="input"))[0]["uid"]
        self.assertNotIn("Error", await s.click(field))
        self.assertEqual(
            await s._evaluate(connection, "document.activeElement.tagName"), "INPUT"
        )
        self.assertNotIn("Error", await s.fill(field, "nested-value"))
        self.assertIn(
            "nested-value",
            await s.evaluate_script(
                "document.querySelector('input').value", wait_for_stable=False
            ),
        )

    async def test_concurrent_console_enable_is_idempotent_and_disable_stops_logs(self):
        page = await s._page_tab()
        page_id = str(page.target_id)
        await asyncio.gather(
            *(s.enable_console_collection(page_id=page_id) for _ in range(3))
        )
        self.assertEqual(len(page.handlers[uc.cdp.runtime.ConsoleAPICalled]), 1)
        await s.evaluate_script(
            "console.log('single-concurrent-log')", wait_for_stable=False
        )
        self.assertEqual(
            sum(m["text"] == "single-concurrent-log" for m in s._console_messages), 1
        )
        await s.disable_console_collection(page_id=page_id)
        self.assertFalse(page.handlers[uc.cdp.runtime.ConsoleAPICalled])
        await s.evaluate_script(
            "console.log('after-concurrent-disable')", wait_for_stable=False
        )
        self.assertFalse(
            any(m["text"] == "after-concurrent-disable" for m in s._console_messages)
        )

    async def test_native_script_cancellation_restores_page_for_busy_and_idle_calls(
        self,
    ):
        page = await s._page_tab()
        for source, format in (
            ("while(true){}", "script"),
            ("() => {while(true){}}", "function"),
            ("async () => {await Promise.resolve();while(true){}}", "auto"),
            ("new Promise(() => {})", "script"),
        ):
            with self.subTest(source=source):
                task = asyncio.create_task(
                    s.evaluate_script(
                        source, format=format, timeout=0, wait_for_stable=False
                    )
                )
                await asyncio.sleep(0.15)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                responsive = False
                try:
                    result = await asyncio.wait_for(
                        s.evaluate_script("42", wait_for_stable=False), 2
                    )
                    self.assertIn("Status: completed", result)
                    self.assertEqual(json.loads(result.splitlines()[1]), 42)
                    responsive = True
                finally:
                    if not responsive:
                        await asyncio.wait_for(
                            page.send(uc.cdp.runtime.terminate_execution()), 1
                        )

    async def test_concurrent_named_context_pages_share_cookies_and_close_together(
        self,
    ):
        browser = await s._get_browser()
        results = await asyncio.gather(
            *(
                s.new_page(self.url, background=True, isolated_context="shared-review")
                for _ in range(2)
            )
        )
        ids = [
            re.search(r"page_id=([^)]*)", result.splitlines()[0]).group(1)
            for result in results
        ]
        pages = [
            next(tab for tab in browser.tabs if str(tab.target_id) == page_id)
            for page_id in ids
        ]
        self.assertEqual(
            pages[0].target.browser_context_id, pages[1].target.browser_context_id
        )
        self.assertEqual(
            pages[0].target.browser_context_id,
            s._named_browser_contexts["shared-review"],
        )
        await s.set_cookie("shared-review-cookie", "yes", "127.0.0.1", page_id=ids[0])
        self.assertIn("shared-review-cookie", await s.get_cookies(page_id=ids[1]))
        await self.tool("close_browser_context", name="shared-review")
        await browser.update_targets()
        self.assertFalse(any(str(tab.target_id) in ids for tab in browser.tabs))
        self.assertNotIn("shared-review", s._named_browser_contexts)

    async def test_concurrent_explicit_context_creation_rejects_duplicate_name(self):
        results = await asyncio.gather(
            *(
                self.tool("create_browser_context", name="explicit-review")
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(result, ValueError) for result in results), 1)
        self.assertEqual(
            sum(isinstance(result, str) and "Created" in result for result in results),
            1,
        )
        browser = await s._get_browser()
        contexts, _ = await browser.send(uc.cdp.target.get_browser_contexts())
        self.assertEqual(
            [str(context) for context in contexts],
            [str(s._named_browser_contexts["explicit-review"])],
        )
        await self.tool("close_browser_context", name="explicit-review")

    async def test_start_browser_reuses_actual_default_context_and_selected_page(self):
        browser = await s._get_browser()
        await browser.update_targets()
        count = len(browser.tabs)
        selected = str((await s._page_tab()).target_id)
        for url in (self.url + "interaction", ""):
            result = await s.start_browser(url=url)
            self.assertEqual(result["status"], "reused")
            self.assertEqual(result["page_id"], selected)
            self.assertEqual(result["url"], self.url + "interaction")
            self.assertEqual(len(browser.tabs), count)
        await self.tool("create_browser_context", name="isolated-review")
        result = await s.start_browser()
        self.assertEqual(
            result["page_id"],
            selected if count == 1 else str(browser.main_tab.target_id),
        )
        self.assertEqual(
            (await s._page_tab()).target.browser_context_id,
            s._default_browser_context_id,
        )

    async def test_frame_runtime_cleanup_serializes_new_event_subscriptions(self):
        page = await s._page_tab()
        frames = await self.tool("list_frames")
        child = next(frame for frame in frames if frame["name"] == "child")
        await self.tool("select_frame", frame_id=child["frame_id"])
        for console_shutdown in (False, True):
            with self.subTest(console_shutdown=console_shutdown):
                if console_shutdown:
                    await s.enable_console_collection()
                entered, released = asyncio.Event(), asyncio.Event()
                native_send = page.send

                async def send(command, *args, **kwargs):
                    packet = next(command)

                    def replay():
                        response = yield packet
                        try:
                            command.send(response)
                        except StopIteration as stop:
                            return stop.value

                    result = await native_send(replay(), *args, **kwargs)
                    if packet["method"] == (
                        "Runtime.disable"
                        if console_shutdown
                        else "Runtime.releaseObjectGroup"
                    ):
                        entered.set()
                        await released.wait()
                    return result

                pending = []
                with patch.object(page, "send", send):
                    operation = asyncio.create_task(
                        s.disable_console_collection()
                        if console_shutdown
                        else s.evaluate_script("document.URL", wait_for_stable=False)
                    )
                    pending.append(operation)
                    try:
                        await asyncio.wait_for(entered.wait(), 2)
                        subscription = asyncio.create_task(
                            self.tool(
                                "subscribe_cdp_events",
                                events=["Runtime.ConsoleAPICalled"],
                            )
                        )
                        pending.append(subscription)
                        await asyncio.sleep(0.03)
                        self.assertFalse(subscription.done())
                        released.set()
                        outcome, identifier = await asyncio.gather(
                            operation, subscription
                        )
                        self.assertNotIn("Error", outcome)
                    finally:
                        released.set()
                        for task in pending:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                result = await s.evaluate_script("document.URL", wait_for_stable=False)
                self.assertIn("Status: completed", result)
                marker = "subscription-after-cleanup-" + str(console_shutdown)
                await s.evaluate_script(
                    f"console.log({json.dumps(marker)})", wait_for_stable=False
                )
                events = await self.tool("get_cdp_events", subscription_id=identifier)
                self.assertTrue(any(marker in json.dumps(event) for event in events))
                await self.tool("unsubscribe_cdp_events", subscription_id=identifier)

    async def test_frame_lookup_preserves_explicit_runtime_domain_ownership(self):
        page = await s._page_tab()
        await self.tool("send_cdp_command", domain="Runtime", command="enable")
        self.assertTrue(s._frame_contexts.state(page)["runtime_requested"])
        child = next(
            frame
            for frame in await self.tool("list_frames")
            if frame["name"] == "child"
        )
        await self.tool("select_frame", frame_id=child["frame_id"])
        for _ in range(2):
            result = await s.evaluate_script("document.URL", wait_for_stable=False)
            self.assertIn("Status: completed", result)
            self.assertTrue(s._frame_contexts.state(page)["contexts"])
        await s.enable_console_collection()
        await s.disable_console_collection()
        self.assertTrue(s._frame_contexts.state(page)["contexts"])
        await self.tool("send_cdp_command", domain="Runtime", command="disable")
        self.assertFalse(s._frame_contexts.state(page)["runtime_requested"])
        self.assertFalse(s._frame_contexts.state(page)["contexts"])
        self.assertIn(
            "Status: completed",
            await s.evaluate_script("document.URL", wait_for_stable=False),
        )

    async def test_failed_runtime_disable_recovers_same_process_iframe_world(self):
        page = await s._page_tab()
        child = next(
            frame
            for frame in await self.tool("list_frames")
            if frame["name"] == "child"
        )
        await self.tool("select_frame", frame_id=child["frame_id"])
        await s.enable_console_collection()
        await s.evaluate_script(
            "window.runtimeRecoveryMarker=42", format="script", wait_for_stable=False
        )
        for applied in (False, True):
            for cancelled in (False, True):
                with self.subTest(applied=applied, cancelled=cancelled):
                    entered = asyncio.Event()
                    scopes = []
                    native_send = page.send

                    async def send(command, *args, **kwargs):
                        if command.gi_code.co_name == "disable":
                            if applied:
                                await native_send(command, *args, **kwargs)
                            entered.set()
                            await asyncio.Future()
                        return await native_send(command, *args, **kwargs)

                    async def request():
                        with anyio.CancelScope() as scope:
                            scopes.append(scope)
                            await self.tool(
                                "send_cdp_command",
                                domain="Runtime",
                                command="disable",
                                timeout=0 if cancelled else 100,
                            )

                    with patch.object(page, "send", send):
                        task = asyncio.create_task(request())
                        try:
                            await asyncio.wait_for(entered.wait(), 2)
                            if cancelled:
                                scopes[0].cancel()
                                await asyncio.wait_for(task, 2)
                            else:
                                with self.assertRaises(TimeoutError):
                                    await asyncio.wait_for(task, 2)
                        finally:
                            if not task.done():
                                task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                    result = await s.evaluate_script(
                        "[runtimeRecoveryMarker,document.URL]", wait_for_stable=False
                    )
                    self.assertIn("Status: completed", result)
                    self.assertEqual(
                        json.loads(result.splitlines()[1]), [42, self.url + "frame"]
                    )
                    marker = f"runtime-recovered-{applied}-{cancelled}"
                    await s.evaluate_script(
                        f"console.log({json.dumps(marker)})", wait_for_stable=False
                    )
                    self.assertIn(marker, await s.list_console_messages())
