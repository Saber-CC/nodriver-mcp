import collections
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
