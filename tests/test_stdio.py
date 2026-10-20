import asyncio
import base64
import json
import io
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image as PillowImage

from mcp import ClientSession, StdioServerParameters
from mcp.client import Client
from mcp.client.stdio import stdio_client

from nodriver_mcp.tool_groups import ENABLED_GROUPS_ENV, TOOL_GROUP_BY_NAME


class StdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_client_discovers_and_calls_official_binding_tools(self):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "nodriver_mcp"],
            env={**os.environ, ENABLED_GROUPS_ENV: ""},
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                self.assertEqual(initialized.server_info.name, "nodriver-mcp")
                self.assertTrue(initialized.server_info.version)
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                self.assertNotIn("find_in_page", names)
                self.assertEqual(len(names), 60)
                self.assertTrue(names.isdisjoint(TOOL_GROUP_BY_NAME))
                self.assertTrue(
                    {
                        "take_screenshot",
                        "set_cookies",
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
                disabled = await session.call_tool("list_webmcp_tools", {})
                self.assertTrue(disabled.is_error)
                self.assertIn(
                    "disabled",
                    "".join(getattr(c, "text", "") for c in disabled.content),
                )

    async def test_modern_client_discovers_and_calls_tools(self):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "nodriver_mcp"],
            env={**os.environ, ENABLED_GROUPS_ENV: ""},
        )
        async with Client(parameters, mode="2026-07-28") as client:
            self.assertEqual(client.protocol_version, "2026-07-28")
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            self.assertEqual(len(names), 60)
            self.assertIn("start_browser", names)
            self.assertTrue(names.isdisjoint(TOOL_GROUP_BY_NAME))
            response = await client.call_tool(
                "list_cdp_commands", {"domain": "ServiceWorker"}
            )
            self.assertFalse(response.is_error, str(response))
            self.assertIn(
                "enable", "".join(getattr(c, "text", "") for c in response.content)
            )

    async def test_all_optional_groups_can_be_restored_after_restart(self):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "nodriver_mcp"],
            env={**os.environ, ENABLED_GROUPS_ENV: "all"},
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                self.assertEqual(len(names), 75)
                self.assertTrue(TOOL_GROUP_BY_NAME.keys() <= names)
                response = await session.call_tool(
                    "performance_analyze_trace",
                    {"trace_path": "/nonexistent-optional-group-trace.json"},
                )
                self.assertTrue(response.is_error)
                self.assertIn(
                    "Trace file does not exist",
                    "".join(getattr(c, "text", "") for c in response.content),
                )

    async def test_modern_client_can_enable_only_one_optional_group(self):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "nodriver_mcp"],
            env={**os.environ, ENABLED_GROUPS_ENV: "diagnostics"},
        )
        async with Client(parameters, mode="2026-07-28") as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            self.assertEqual(len(names), 64)
            self.assertIn("get_connection_history", names)
            self.assertNotIn("performance_start_trace", names)
            self.assertNotIn("call_webmcp_tool", names)
            response = await client.call_tool("list_webmcp_tools", {})
            self.assertTrue(response.is_error)
            self.assertIn(
                "disabled", "".join(getattr(c, "text", "") for c in response.content)
            )


@unittest.skipUnless(
    os.environ.get("NODRIVER_MCP_LIVE_TESTS") == "1", "Chrome integration is opt-in"
)
class StdioCancellationTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def session(self, directory):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                str(Path(__file__).with_name("stdio_cancellation_server.py")),
                str(directory),
            ],
            env={
                **os.environ,
                "NODRIVER_HEADLESS": "true",
                "NODRIVER_USER_DATA_DIR": str(directory / "profile"),
                "NODRIVER_PROXY": "",
                "NODRIVER_EXTENSIONS": "[]",
                "NODRIVER_BROWSER_ARGS": "[]",
                ENABLED_GROUPS_ENV: "",
            },
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                try:
                    await self.call(
                        session,
                        "start_browser",
                        url="data:text/html,<title>MCP cancellation</title><input>",
                    )
                    yield session
                finally:
                    (directory / "release-version").touch()
                    await self.call(session, "close_browser")

    async def call(self, session, name, **arguments):
        result = await asyncio.wait_for(session.call_tool(name, arguments), 15)
        self.assertFalse(result.is_error, str(result))
        return "".join(getattr(item, "text", "") for item in result.content)

    async def wait_file(self, path):
        async with asyncio.timeout(5):
            while not path.exists():
                await asyncio.sleep(0.01)

    async def test_frame_evaluation_preserves_lexicals_without_runtime_console_serialization(
        self,
    ):
        with tempfile.TemporaryDirectory(
            prefix="nodriver-mcp-frame-world-"
        ) as temporary:
            async with self.session(Path(temporary)) as session:
                setup = await self.call(
                    session,
                    "evaluate_script",
                    function="""new Promise(resolve=>{
                    window.runtimeEnableLeakDetected=false;
                    const error=new Error('stdio-runtime-probe');
                    Object.defineProperty(error,'name',{get(){window.runtimeEnableLeakDetected=true;return 'Error'}});
                    console.log(error);
                    window.runtimeEnableLeakDetected=false;
                    const frame=document.createElement('iframe');
                    frame.name='stdio-frame';frame.srcdoc='<input aria-label="Frame field"><p>frame marker</p>';
                    frame.onload=()=>resolve(true);document.body.append(frame);
                })""",
                    format="script",
                    wait_for_stable=False,
                )
                self.assertIn("Status: completed", setup)
                frames = await session.call_tool("list_frames", {})
                self.assertFalse(frames.is_error, str(frames))
                child = next(
                    frame
                    for frame in frames.structured_content["result"]
                    if frame["name"] == "stdio-frame"
                )
                await self.call(session, "select_frame", frame_id=child["frame_id"])
                await self.call(
                    session,
                    "evaluate_script",
                    function="let lexicalFrameProbe=21;const constFrameProbe=42;",
                    format="script",
                    wait_for_stable=False,
                )
                value = await self.call(
                    session,
                    "evaluate_script",
                    function="[lexicalFrameProbe,constFrameProbe]",
                    wait_for_stable=False,
                )
                self.assertEqual(json.loads(value.splitlines()[1]), [21, 42])
                await self.call(
                    session, "wait_for", text=["frame marker"], timeout=2000
                )
                await self.call(session, "select_frame", frame_id="")
                flag = await self.call(
                    session,
                    "evaluate_script",
                    function="runtimeEnableLeakDetected",
                    wait_for_stable=False,
                )
                self.assertFalse(json.loads(flag.splitlines()[1]))
                clean = await self.call(
                    session,
                    "evaluate_script",
                    function="[window,document.querySelector('iframe').contentWindow].map(w=>Object.getOwnPropertyNames(w).filter(n=>n.startsWith('__mcp_context_')))",
                    wait_for_stable=False,
                )
                self.assertEqual(json.loads(clean.splitlines()[1]), [[], []])
                await self.call(
                    session, "send_cdp_command", domain="Runtime", command="enable"
                )
                flag = await self.call(
                    session,
                    "evaluate_script",
                    function="runtimeEnableLeakDetected",
                    wait_for_stable=False,
                )
                self.assertTrue(json.loads(flag.splitlines()[1]))

    async def test_real_request_cancellation_stops_busy_js_and_preserves_idle_promises(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix="nodriver-mcp-cancel-") as temporary:
            directory = Path(temporary)
            async with self.session(directory) as session:
                await self.call(session, "enable_console_collection")
                snapshot = await self.call(session, "take_snapshot")
                uid = re.search(r"uid=(\S+) textbox", snapshot).group(1)
                for args in (None, [uid]):
                    for busy in (True, False):
                        with self.subTest(args=args, busy=busy):
                            finished = directory / "script-cancelled"
                            finished.unlink(missing_ok=True)
                            marker = f"started-{bool(args)}-{busy}"
                            source = f"console.log({json.dumps(marker)});"
                            source += "while(true){}" if busy else "new Promise(()=>{})"
                            if args:
                                source = (
                                    "el=>{"
                                    + source.replace(
                                        "new Promise", "return new Promise"
                                    )
                                    + "}"
                                )
                            pending = asyncio.create_task(
                                session.call_tool(
                                    "evaluate_script",
                                    {
                                        "function": source,
                                        "format": "function" if args else "script",
                                        "args": args,
                                        "timeout": 0,
                                        "wait_for_stable": False,
                                    },
                                )
                            )
                            responsive = False
                            try:
                                async with asyncio.timeout(5):
                                    while marker not in await self.call(
                                        session, "list_console_messages"
                                    ):
                                        await asyncio.sleep(0.01)
                                pending.cancel()
                                with self.assertRaises(asyncio.CancelledError):
                                    await pending
                                await self.wait_file(finished)
                                for _ in range(2):
                                    result = await self.call(
                                        session,
                                        "evaluate_script",
                                        function="42",
                                        format="script",
                                        timeout=1000,
                                        wait_for_stable=False,
                                    )
                                    self.assertIn("Status: completed", result)
                                    self.assertEqual(
                                        json.loads(result.splitlines()[1]), 42
                                    )
                                responsive = True
                            finally:
                                pending.cancel()
                                await asyncio.gather(pending, return_exceptions=True)
                                if not responsive:
                                    await self.call(
                                        session,
                                        "send_cdp_command",
                                        domain="Runtime",
                                        command="terminateExecution",
                                    )

    async def test_real_request_cancellation_closes_version_target_created_before_reply(
        self,
    ):
        with tempfile.TemporaryDirectory(
            prefix="nodriver-mcp-version-cancel-"
        ) as temporary:
            directory = Path(temporary)
            async with self.session(directory) as session:
                pending = asyncio.create_task(
                    session.call_tool(
                        "get_browser_info", {"include_version_page": True}
                    )
                )
                try:
                    await self.wait_file(directory / "version-created")
                    pending.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await pending
                    await self.wait_file(directory / "version-cancelled")
                    (directory / "release-version").touch()
                    await self.wait_file(directory / "version-finished")
                    pages = await self.call(session, "list_pages")
                    self.assertNotIn("chrome://version", pages)
                    self.assertNotIn((directory / "version-created").read_text(), pages)
                finally:
                    (directory / "release-version").touch()
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)


@unittest.skipUnless(
    os.environ.get("NODRIVER_MCP_LIVE_TESTS") == "1", "Chrome integration is opt-in"
)
class StdioSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_client_operates_on_two_pages_concurrently(self):
        class Page(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = (
                    "<title>Concurrent MCP</title><input aria-label='Field'>"
                    "<input type='checkbox' aria-label='Toggle'>"
                    "<input id='upload' type='file' multiple aria-label='Upload'>"
                    f"<p>{self.path}</p>"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        http = ThreadingHTTPServer(("127.0.0.1", 0), Page)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{http.server_port}"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "nodriver_mcp"],
            env={
                **os.environ,
                "NODRIVER_HEADLESS": "true",
                "NODRIVER_PROXY": "",
                "NODRIVER_EXTENSIONS": "[]",
                "NODRIVER_BROWSER_ARGS": '["--enable-unsafe-extension-debugging"]',
                ENABLED_GROUPS_ENV: "performance",
            },
        )

        async def call(session, name, **arguments):
            result = await session.call_tool(name, arguments)
            self.assertFalse(result.is_error, str(result))
            return "".join(getattr(c, "text", "") for c in result.content)

        try:
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = {
                        tool.name: tool for tool in (await session.list_tools()).tools
                    }
                    self.assertIn("page_id", tools["fill"].input_schema["properties"])
                    a = json.loads(
                        await call(
                            session, "start_browser", url=url + "/alpha", proxy=""
                        )
                    )["page_id"]
                    opened = await call(
                        session, "new_page", url=url + "/beta", background=True
                    )
                    b = re.search(r"page_id=(\w+)", opened).group(1)
                    snapshots = await asyncio.gather(
                        *(
                            call(session, "take_snapshot", page_id=page)
                            for page in (a, b)
                        )
                    )
                    uids = [
                        re.search(r'uid=(\S+) textbox "Field"', text).group(1)
                        for text in snapshots
                    ]
                    filled = await asyncio.gather(
                        call(
                            session,
                            "fill_form",
                            elements=[{"uid": uids[0], "value": "client-alpha"}],
                            include_snapshot=True,
                            page_id=a,
                        ),
                        call(
                            session,
                            "fill_form",
                            elements=[{"uid": uids[1], "value": "client-beta"}],
                            include_snapshot=True,
                            page_id=b,
                        ),
                    )
                    self.assertIn("client-alpha", filled[0])
                    self.assertNotIn("client-beta", filled[0])
                    self.assertIn("client-beta", filled[1])
                    values = await asyncio.gather(
                        *(
                            call(
                                session,
                                "evaluate_script",
                                function="document.querySelector('input').value",
                                page_id=page,
                            )
                            for page in (a, b)
                        )
                    )
                    self.assertIn("client-alpha", values[0])
                    self.assertIn("client-beta", values[1])
                    toggle_snapshot = await call(session, "take_snapshot", page_id=a)
                    toggle = re.search(
                        r'uid=(\S+) checkbox "Toggle"', toggle_snapshot
                    ).group(1)
                    await call(session, "fill", uid=toggle, value=True, page_id=a)
                    self.assertIn(
                        "true",
                        await call(
                            session,
                            "evaluate_script",
                            function="document.querySelector('[type=checkbox]').checked",
                            wait_for_stable=False,
                            page_id=a,
                        ),
                    )
                    invalid = await session.call_tool(
                        "fill", {"uid": toggle, "value": 1, "page_id": a}
                    )
                    self.assertTrue(invalid.is_error)
                    with tempfile.TemporaryDirectory() as upload_dir:
                        first = os.path.join(upload_dir, "client-one.txt")
                        second = os.path.join(upload_dir, "client-two.txt")
                        for path in (first, second):
                            with open(path, "w", encoding="utf-8") as file:
                                file.write("MCP file upload")
                        snapshot = await call(session, "take_snapshot", page_id=a)
                        upload = re.search(
                            r'uid=(\S+) button "Upload"', snapshot
                        ).group(1)
                        await call(
                            session,
                            "upload_file",
                            uid=upload,
                            file_paths=[first, second],
                            page_id=a,
                        )
                        self.assertIn(
                            "2",
                            await call(
                                session,
                                "evaluate_script",
                                function="document.getElementById('upload').files.length",
                                wait_for_stable=False,
                                page_id=a,
                            ),
                        )
                    await call(session, "enable_console_collection", page_id=a)
                    await call(
                        session,
                        "evaluate_script",
                        function="() => {setTimeout(() => {throw new Error('client-uncaught')},0); return true;}",
                        wait_for_stable=False,
                        page_id=a,
                    )
                    messages = await call(
                        session,
                        "list_console_messages",
                        types=["error"],
                        include_stack_traces=True,
                        page_id=a,
                    )
                    message = re.search(
                        r"\[(\d+)\] \[error\].*client-uncaught", messages
                    ).group(1)
                    details = await call(
                        session, "get_console_message", msgid=int(message), page_id=a
                    )
                    self.assertIn("Stack trace", details)
                    self.assertIn('"source": "exception"', details)
                    await call(session, "disable_console_collection", page_id=a)
                    prompt = await call(
                        session,
                        "evaluate_script",
                        function="() => prompt('client prompt')",
                        prompt_text="client-text",
                        wait_for_stable=False,
                        page_id=a,
                    )
                    self.assertIn("Status: completed", prompt)
                    self.assertIn("client-text", prompt)
                    manual = await call(
                        session,
                        "evaluate_script",
                        function="() => confirm('client manual')",
                        dialog_action="manual",
                        wait_for_stable=False,
                        page_id=a,
                    )
                    self.assertIn("Status: interrupted", manual)
                    blocked = await session.call_tool("take_snapshot", {"page_id": a})
                    self.assertTrue(blocked.is_error)
                    await call(session, "handle_dialog", action="dismiss", page_id=a)
                    timed_out = await call(
                        session,
                        "evaluate_script",
                        function="new Promise(() => {})",
                        timeout=100,
                        wait_for_stable=False,
                        page_id=a,
                    )
                    self.assertIn("Status: timed_out", timed_out)
                    latest = await call(session, "take_snapshot", page_id=a)
                    field = re.search(r'uid=(\S+) textbox "Field"', latest).group(1)
                    bulk = "MCP 中文🙂" * 100
                    self.assertNotIn(
                        "Error",
                        await call(session, "fill", uid=field, value=bulk, page_id=a),
                    )
                    self.assertIn(
                        bulk,
                        await call(
                            session,
                            "evaluate_script",
                            function="document.querySelector('input').value",
                            wait_for_stable=False,
                            page_id=a,
                        ),
                    )
                    partial = await call(
                        session,
                        "fill_form",
                        elements=[
                            {"uid": field, "value": "one-before-error"},
                            {"uid": "stale-client-uid", "value": "bad"},
                            {"uid": field, "value": "must-not-run"},
                        ],
                        page_id=a,
                    )
                    self.assertIn("Completed 1 of 3", partial)
                    self.assertIn("Skipped 1", partial)
                    self.assertIn(
                        "one-before-error",
                        await call(
                            session,
                            "evaluate_script",
                            function="document.querySelector('input').value",
                            wait_for_stable=False,
                            page_id=a,
                        ),
                    )
                    await call(
                        session, "fill", uid=field, value="client-alpha", page_id=a
                    )
                    with tempfile.TemporaryDirectory() as result_dir:
                        output = os.path.join(result_dir, "result 文件.json")
                        saved = await call(
                            session,
                            "evaluate_script",
                            function="(() => ({rows:Array.from({length:500},(_,i)=>i)}))()",
                            file_path=output,
                            max_output_chars=100,
                            wait_for_stable=False,
                            page_id=a,
                        )
                        self.assertIn("Preview truncated", saved)
                        with open(output, encoding="utf-8") as file:
                            self.assertEqual(json.load(file)["rows"][-1], 499)
                    await call(
                        session,
                        "enable_console_collection",
                        source_maps=True,
                        page_id=a,
                    )
                    source_map = {
                        "version": 3,
                        "sourceRoot": "/src/",
                        "sources": ["original-client.ts"],
                        "names": ["clientOriginal"],
                        "mappings": "AAIEA",
                    }
                    encoded = base64.b64encode(json.dumps(source_map).encode()).decode()
                    script = (
                        "console.error('source-map-client');\n//# sourceMappingURL=data:application/json;base64,"
                        + encoded
                        + "\n//# sourceURL="
                        + url
                        + "/client-source.js"
                    )
                    await call(
                        session,
                        "evaluate_script",
                        function=script,
                        format="script",
                        wait_for_stable=False,
                        page_id=a,
                    )
                    messages = await call(session, "list_console_messages", page_id=a)
                    msgid = int(
                        re.search(
                            r"\[(\d+)\] \[error\] source-map-client", messages
                        ).group(1)
                    )
                    mapped = await call(
                        session, "get_console_message", msgid=msgid, page_id=a
                    )
                    self.assertIn("/src/original-client.ts:5:3", mapped)
                    self.assertIn("clientOriginal", mapped)
                    await call(
                        session,
                        "enable_console_collection",
                        source_maps=False,
                        page_id=a,
                    )
                    with tempfile.TemporaryDirectory() as extension_dir:
                        extension = Path(extension_dir)
                        (extension / "manifest.json").write_text(
                            json.dumps(
                                {
                                    "manifest_version": 3,
                                    "name": "MCP worker",
                                    "version": "1.0",
                                    "background": {"service_worker": "sw.js"},
                                }
                            )
                        )
                        (extension / "sw.js").write_text(
                            "globalThis.workerClientValue='client-worker';chrome.runtime.onInstalled.addListener(()=>{});"
                        )
                        loaded = await call(
                            session,
                            "send_cdp_command",
                            domain="Extensions",
                            command="loadUnpacked",
                            parameters={"path": str(extension)},
                            scope="browser",
                        )
                        extension_id = loaded.strip('"')
                        async with asyncio.timeout(10):
                            while True:
                                targets = await call(
                                    session, "list_pages", include_service_workers=True
                                )
                                lines = [
                                    line
                                    for line in targets.splitlines()
                                    if "service_worker_id" in line
                                    and extension_id in line
                                ]
                                if lines:
                                    worker_id = json.loads(lines[0].strip())[
                                        "service_worker_id"
                                    ]
                                    break
                                await asyncio.sleep(0.05)
                        self.assertIn(
                            "client-worker",
                            await call(
                                session,
                                "evaluate_script",
                                function="workerClientValue",
                                service_worker_id=worker_id,
                            ),
                        )
                        await call(
                            session,
                            "enable_console_collection",
                            service_worker_id=worker_id,
                        )
                        await call(
                            session,
                            "evaluate_script",
                            function="console.log('client-worker-log')",
                            service_worker_id=worker_id,
                        )
                        self.assertIn(
                            "client-worker-log",
                            await call(
                                session,
                                "list_console_messages",
                                service_worker_id=worker_id,
                            ),
                        )
                        failure = await session.call_tool(
                            "evaluate_script",
                            {
                                "function": "1",
                                "service_worker_id": worker_id,
                                "page_id": a,
                            },
                        )
                        self.assertTrue(failure.is_error)
                        await call(
                            session,
                            "disable_console_collection",
                            service_worker_id=worker_id,
                        )
                        await call(
                            session,
                            "send_cdp_command",
                            domain="Extensions",
                            command="uninstall",
                            parameters={"id": extension_id},
                            scope="browser",
                        )
                    with tempfile.TemporaryDirectory() as script_dir:
                        source = Path(script_dir) / "client 脚本.js"
                        source.write_text(
                            '() => "source-file-client"', encoding="utf-8"
                        )
                        self.assertIn(
                            "source-file-client",
                            await call(
                                session,
                                "evaluate_script",
                                source_path=str(source),
                                wait_for_stable=False,
                                page_id=a,
                            ),
                        )
                        failure = await session.call_tool(
                            "evaluate_script",
                            {"function": "1", "source_path": str(source), "page_id": a},
                        )
                        self.assertTrue(failure.is_error)
                        self.assertIn(
                            "exactly one",
                            "".join(getattr(c, "text", "") for c in failure.content),
                        )
                    await call(
                        session,
                        "emulate",
                        prefers_reduced_motion="reduce",
                        forced_colors="none",
                        extra_http_headers={"X-Client": "media"},
                        page_id=a,
                    )
                    self.assertIn(
                        "true",
                        await call(
                            session,
                            "evaluate_script",
                            function="matchMedia('(prefers-reduced-motion:reduce)').matches",
                            wait_for_stable=False,
                            page_id=a,
                        ),
                    )
                    await call(
                        session,
                        "emulate",
                        prefers_reduced_motion="auto",
                        extra_http_headers={},
                        page_id=a,
                    )
                    with tempfile.TemporaryDirectory() as trace_dir:
                        trace_path = os.path.join(trace_dir, "client-trace.json")
                        report = await call(
                            session,
                            "performance_start_trace",
                            reload=False,
                            duration_ms=50,
                            file_path=trace_path,
                            page_id=a,
                        )
                        info = json.loads(report.partition("\n")[2])
                        self.assertEqual(
                            Path(info["file_path"]), Path(trace_path).resolve()
                        )
                        with open(trace_path) as file:
                            self.assertEqual(
                                info["event_count"], len(json.load(file)["traceEvents"])
                            )
                        if os.environ.get("NODRIVER_MCP_PERFORMANCE_TESTS") == "1":
                            fixture = (
                                Path(__file__).parent
                                / "fixtures/web-dev-with-commit.json.gz"
                            )
                            analysis_path = Path(trace_dir) / "client-analysis.json"
                            analysis = await call(
                                session,
                                "performance_analyze_trace",
                                trace_path=str(fixture),
                                file_path=str(analysis_path),
                                max_output_chars=100,
                            )
                            self.assertIn("preview truncated", analysis)
                            payload = json.loads(analysis_path.read_text())
                            self.assertAlmostEqual(
                                payload["web_vitals"][0]["lcp_ms"], 129.211
                            )
                            detail = await call(
                                session,
                                "performance_analyze_insight",
                                trace_path=str(fixture),
                                insight_set_id=payload["insight_sets"][0]["id"],
                                insight_name="LCPBreakdown",
                                file_path=str(analysis_path),
                                max_output_chars=10000,
                            )
                            self.assertIn("Time to first byte", detail)

                    await call(
                        session,
                        "evaluate_script",
                        function="fetch('/body-probe').then(r=>r.text())",
                        wait_for_stable=False,
                        page_id=a,
                    )
                    requests = await call(
                        session,
                        "list_network_requests",
                        url_filter="/body-probe",
                        page_id=a,
                    )
                    reqid = int(re.search(r"\[(\d+)\]", requests).group(1))
                    detail = await call(
                        session,
                        "get_network_request",
                        reqid=reqid,
                        max_body_chars=10,
                        page_id=a,
                    )
                    self.assertIn("preview truncated", detail)
                    self.assertIn('"response_body_state": "available"', detail)
                    self.assertIn(
                        "not_requested",
                        await call(
                            session,
                            "get_network_request",
                            reqid=reqid,
                            include_body=False,
                            page_id=a,
                        ),
                    )
                    raw = await call(
                        session,
                        "evaluate_script",
                        function="var cliFormatResult=42;cliFormatResult",
                        format="script",
                        wait_for_stable=False,
                        page_id=a,
                    )
                    self.assertEqual(json.loads(raw.splitlines()[1]), 42)
                    self.assertIn(
                        "client-alpha",
                        await call(
                            session,
                            "evaluate_script",
                            function="document.querySelector('input').value",
                        ),
                    )
                    await call(session, "close_page", page_id=b)
                    failure = await session.call_tool("take_snapshot", {"page_id": b})
                    self.assertTrue(failure.is_error)
                    self.assertIn(
                        "closed page_id",
                        "".join(getattr(c, "text", "") for c in failure.content),
                    )
        finally:
            http.shutdown()
            http.server_close()
            thread.join()

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
                        limited = await session.call_tool(
                            "take_screenshot",
                            {"format": "webp", "max_width": 137, "max_height": 97},
                        )
                        self.assertFalse(limited.is_error, str(limited))
                        image = next(
                            item for item in limited.content if item.type == "image"
                        )
                        self.assertEqual(image.mime_type, "image/webp")
                        decoded = PillowImage.open(
                            io.BytesIO(base64.b64decode(image.data))
                        )
                        self.assertLessEqual(decoded.width, 137)
                        self.assertLessEqual(decoded.height, 97)
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
