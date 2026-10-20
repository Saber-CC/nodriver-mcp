import asyncio
import collections
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nodriver import cdp
from nodriver_mcp.console import ConsoleCollector, format_message


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        handlers = collections.defaultdict(list)

        def remove(kind, callback):
            if callback in handlers[kind]:
                handlers[kind].remove(callback)

        connection = SimpleNamespace(
            send=AsyncMock(),
            handlers=handlers,
            add_handler=lambda kind, callback: handlers[kind].append(callback),
            remove_handler=remove,
        )
        records = []

        def collect(record):
            record.update(connection=connection, msgid=len(records), page_id="page")
            records.append(record)

        return (
            ConsoleCollector(connection, collect, lambda: records),
            connection,
            handlers,
            records,
        )

    async def test_replayed_console_binds_script_metadata_before_runtime_enable(self):
        collector, connection, _, records = self.fixture()

        async def send(command):
            method = next(command)["method"]
            if method == "Debugger.enable":
                collector.source_maps.parsed(
                    SimpleNamespace(
                        script_id="10",
                        execution_context_id=2,
                        hash_="compiled",
                        execution_context_aux_data={"frameId": "page"},
                        url="https://page/app.js",
                        start_line=0,
                        start_column=0,
                        source_map_url="data:application/json,"
                        + json.dumps(
                            {
                                "version": 3,
                                "sources": ["original.ts"],
                                "names": [],
                                "mappings": "AAAA",
                                "sourcesContent": ["original line"],
                            }
                        ),
                    )
                )
            elif method == "Runtime.enable":
                collector.console(
                    cdp.runtime.ConsoleAPICalled.from_json(
                        {
                            "type": "log",
                            "executionContextId": 2,
                            "timestamp": 1,
                            "args": [{"type": "string", "value": "buffered log"}],
                            "stackTrace": {
                                "callFrames": [
                                    {
                                        "scriptId": "10",
                                        "functionName": "minified",
                                        "url": "https://page/app.js",
                                        "lineNumber": 0,
                                        "columnNumber": 0,
                                    }
                                ]
                            },
                        }
                    )
                )

        connection.send.side_effect = send
        await collector.enable(source_maps=True)
        self.assertEqual(len(records), 1)
        detail = await collector.source_maps.symbolize(records[0])
        self.assertEqual(detail["mapped_location"]["url"], "https://page/original.ts")
        self.assertEqual(detail["mapped_location"]["sourceLine"], "original line")
        await collector.source_maps.disable()
        collector.remove()

    async def test_source_map_startup_failure_disables_debugger_and_allows_retry(self):
        for failing in ("Runtime.enable", "Debugger.setSkipAllPauses"):
            with self.subTest(failing=failing):
                collector, connection, handlers, _ = self.fixture()
                external = lambda event: None
                connection.add_handler(cdp.runtime.ExceptionThrown, external)
                commands = []
                error = RuntimeError("startup failed")

                async def send(command):
                    method = next(command)["method"]
                    commands.append(method)
                    if method == failing:
                        raise error

                connection.send.side_effect = send
                with self.assertRaises(RuntimeError) as raised:
                    await collector.enable(source_maps=True)
                self.assertIs(raised.exception, error)
                self.assertFalse(collector.source_maps.active)
                self.assertFalse(collector.source_maps.watching)
                self.assertEqual(commands[-1], "Debugger.disable")
                self.assertEqual(handlers[cdp.runtime.ExceptionThrown], [external])
                self.assertFalse(
                    any(
                        callbacks
                        for kind, callbacks in handlers.items()
                        if kind is not cdp.runtime.ExceptionThrown
                    )
                )
                connection.send.side_effect = None
                await collector.enable(source_maps=True)
                self.assertTrue(collector.source_maps.active)
                await collector.source_maps.disable()
                collector.remove()

    async def test_source_map_cancellation_resets_partial_debugger_and_allows_retry(
        self,
    ):
        for phase in ("Debugger.enable", "Debugger.setSkipAllPauses", "Runtime.enable"):
            with self.subTest(phase=phase):
                collector, connection, handlers, _ = self.fixture()
                entered = asyncio.Event()
                commands = []
                native_enabled = False
                interrupt = True

                async def send(command):
                    nonlocal native_enabled
                    method = next(command)["method"]
                    commands.append(method)
                    if method == "Debugger.enable":
                        native_enabled = True
                    elif method == "Debugger.disable":
                        native_enabled = False
                    if interrupt and method == phase:
                        entered.set()
                        await asyncio.Future()

                connection.send.side_effect = send
                task = asyncio.create_task(collector.enable(source_maps=True))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    self.assertTrue(native_enabled)
                    self.assertEqual(
                        collector.source_maps.enabling, phase == "Debugger.enable"
                    )
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertFalse(native_enabled)
                    self.assertEqual(commands.count("Debugger.disable"), 1)
                    self.assertFalse(collector.source_maps.active)
                    self.assertFalse(collector.source_maps.enabling)
                    self.assertFalse(any(handlers.values()))

                    interrupt = False
                    await collector.enable(source_maps=True)
                    self.assertTrue(native_enabled)
                    self.assertTrue(collector.source_maps.active)
                    self.assertFalse(collector.source_maps.enabling)
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await collector.source_maps.disable()
                    collector.remove()

    async def test_cancelled_source_map_startup_preserves_external_debugger_callbacks(
        self,
    ):
        collector, connection, handlers, _ = self.fixture()
        external = lambda event: None
        connection.add_handler(cdp.debugger.ScriptParsed, external)
        entered = asyncio.Event()
        commands = []

        async def send(command):
            method = next(command)["method"]
            commands.append(method)
            if method == "Debugger.enable":
                entered.set()
                await asyncio.Future()

        connection.send.side_effect = send
        task = asyncio.create_task(collector.enable(source_maps=True))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertNotIn("Debugger.disable", commands)
            self.assertEqual(handlers[cdp.debugger.ScriptParsed], [external])
            self.assertFalse(collector.source_maps.enabling)
            self.assertFalse(collector.source_maps.active)
            self.assertFalse(
                any(
                    callbacks
                    for event, callbacks in handlers.items()
                    if event is not cdp.debugger.ScriptParsed
                )
            )
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            collector.remove()

    async def test_console_and_uncaught_exception_keep_native_locations_and_stacks(
        self,
    ):
        collector, connection, handlers, records = self.fixture()
        await collector.enable()
        stack = {
            "callFrames": [
                {
                    "functionName": "probe",
                    "scriptId": "10",
                    "url": "https://page/app.js",
                    "lineNumber": 4,
                    "columnNumber": 7,
                }
            ]
        }
        collector.console(
            cdp.runtime.ConsoleAPICalled.from_json(
                {
                    "type": "log",
                    "executionContextId": 2,
                    "timestamp": 1,
                    "args": [
                        {"type": "boolean", "value": False},
                        {"type": "number", "value": 0},
                    ],
                    "stackTrace": stack,
                }
            )
        )
        collector.thrown(
            cdp.runtime.ExceptionThrown.from_json(
                {
                    "timestamp": 2,
                    "exceptionDetails": {
                        "exceptionId": 7,
                        "text": "Uncaught (in promise)",
                        "lineNumber": 4,
                        "columnNumber": 7,
                        "executionContextId": 2,
                        "url": "https://page/app.js",
                        "exception": {
                            "type": "object",
                            "description": "Error: rejected\n at probe",
                            "subtype": "error",
                        },
                        "stackTrace": stack,
                    },
                }
            )
        )
        self.assertEqual(records[0]["text"], "False 0")
        self.assertEqual(records[1]["source"], "exception")
        self.assertEqual(records[1]["location"]["line_number"], 4)
        detailed = format_message(records[1])
        self.assertIn("app.js:5:8", detailed)
        self.assertIn("Uncaught (in promise)", detailed)
        self.assertNotIn(
            "Stack trace", format_message(records[1], include_stack_traces=False)
        )
        records.append({"connection": object(), "exception_id": 7})
        collector.revoked(cdp.runtime.ExceptionRevoked("handler added", 7))
        self.assertTrue(records[1]["revoked"])
        self.assertNotIn("revoked", records[-1])
        collector.remove()
        self.assertFalse(any(handlers.values()))

    async def test_enable_failure_removes_managed_handlers_and_preserves_external_ones(
        self,
    ):
        collector, connection, handlers, _ = self.fixture()
        external = lambda event: None
        connection.add_handler(cdp.runtime.ExceptionThrown, external)
        connection.send.side_effect = RuntimeError("disconnected")
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            await collector.enable()
        self.assertEqual(handlers[cdp.runtime.ExceptionThrown], [external])
        self.assertFalse(handlers[cdp.runtime.ConsoleAPICalled])
        self.assertFalse(handlers[cdp.runtime.ExceptionRevoked])
        connection.send.side_effect = None
        await collector.enable()
        collector.remove()
        self.assertEqual(handlers[cdp.runtime.ExceptionThrown], [external])

    async def test_primitive_uncaught_values_are_retained(self):
        collector, _, _, records = self.fixture()
        for value in (False, 0, ""):
            collector.thrown(
                cdp.runtime.ExceptionThrown.from_json(
                    {
                        "timestamp": 1,
                        "exceptionDetails": {
                            "exceptionId": 1,
                            "text": "Uncaught",
                            "lineNumber": 0,
                            "columnNumber": 0,
                            "exception": {"type": "string", "value": value},
                        },
                    }
                )
            )
        self.assertIn("false", records[0]["text"])
        self.assertIn("0", records[1]["text"])
        self.assertIn('""', records[2]["text"])
