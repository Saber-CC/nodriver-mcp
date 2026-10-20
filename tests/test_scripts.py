import json
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anyio
import nodriver as uc
from nodriver_mcp.scripts import (
    execute_script,
    prepare_script_call,
    script_result,
    script_value,
)


class ScriptTests(unittest.IsolatedAsyncioTestCase):
    async def test_document_evaluation_keeps_the_reference_through_probe_and_execution(
        self,
    ):
        commands = []
        group = None

        async def context(owner):
            nonlocal group
            group = owner
            return {
                "document_object_id": uc.cdp.runtime.RemoteObjectId("opaque-document")
            }

        async def send(command):
            packet = next(command)
            commands.append(packet)
            if packet["method"] == "Runtime.callFunctionOn":
                self.assertEqual(packet["params"]["objectId"], "opaque-document")
                self.assertEqual(packet["params"]["objectGroup"], group)
                self.assertNotIn("executionContextId", packet["params"])
                if not packet["params"].get("arguments"):
                    return uc.cdp.runtime.RemoteObject(
                        type_="function",
                        description="function eval() { [native code] }",
                        object_id=uc.cdp.runtime.RemoteObjectId("native-eval"),
                    ), None
                self.assertEqual(
                    packet["params"]["arguments"][1], {"objectId": "native-eval"}
                )
                return uc.cdp.runtime.RemoteObject(type_="boolean", value=True), None

        value = await execute_script(
            SimpleNamespace(send=send),
            "() => true",
            None,
            resolve_context=context,
        )
        self.assertTrue(value)
        self.assertEqual(
            [c["method"] for c in commands],
            [
                "Runtime.callFunctionOn",
                "Runtime.callFunctionOn",
                "Runtime.callFunctionOn",
                "Runtime.releaseObjectGroup",
            ],
        )
        self.assertEqual(commands[-1]["params"]["objectGroup"], group)

    async def test_replaced_document_eval_never_receives_the_script_source(self):
        for evaluator in (
            uc.cdp.runtime.RemoteObject(
                type_="function", description='() => "blocked"', object_id="wrapper"
            ),
            uc.cdp.runtime.RemoteObject(
                type_="object", subtype="proxy", description="Proxy(Function)",
                object_id="proxy",
            ),
            uc.cdp.runtime.RemoteObject(
                type_="function", description="function () { [native code] }",
                object_id="bound-function",
            ),
            uc.cdp.runtime.RemoteObject(type_="undefined"),
        ):
            with self.subTest(evaluator=evaluator.description):
                commands = []

                async def send(command):
                    packet = next(command)
                    commands.append(packet)
                    if packet["method"] == "Runtime.callFunctionOn":
                        return evaluator, None

                with self.assertRaisesRegex(ValueError, "script was not executed"):
                    await execute_script(
                        SimpleNamespace(send=send),
                        "window.sensitiveSourceMarker=42;sensitiveSourceMarker",
                        None,
                        document_object_id=uc.cdp.runtime.RemoteObjectId(
                            "opaque-document"
                        ),
                    )
                self.assertNotIn("sensitiveSourceMarker", json.dumps(commands))
                self.assertEqual(
                    [packet["method"] for packet in commands],
                    ["Runtime.callFunctionOn", "Runtime.releaseObjectGroup"],
                )

    async def test_unrecognized_frame_context_never_falls_back_to_the_main_page(self):
        commands = []

        async def send(command):
            commands.append(next(command))

        with self.assertRaisesRegex(
            ValueError, "Frame execution target is unavailable"
        ):
            await execute_script(
                SimpleNamespace(send=send),
                "window.marker=1",
                None,
                resolve_context=AsyncMock(return_value={"context_id": 3}),
            )
        self.assertEqual(
            [c["method"] for c in commands], ["Runtime.releaseObjectGroup"]
        )

    async def test_document_deadline_and_outer_timeout_preserve_cancellation_cleanup(
        self,
    ):
        commands = []

        async def send(command):
            packet = next(command)
            commands.append(packet)
            if packet["method"] == "Runtime.callFunctionOn":
                await asyncio.Future()

        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(
                execute_script(
                    SimpleNamespace(send=send),
                    "while(true){}",
                    None,
                    format="script",
                    document_object_id=uc.cdp.runtime.RemoteObjectId("opaque-document"),
                    timeout=50,
                ),
                0.05,
            )
        self.assertIn("Runtime.terminateExecution", [c["method"] for c in commands])
        self.assertEqual(commands[-1]["method"], "Runtime.releaseObjectGroup")

    async def test_anyio_request_cancellation_interrupts_busy_js_and_releases_handles(
        self,
    ):
        commands = []
        entered = asyncio.Event()

        async def send(command):
            packet = next(command)
            commands.append(packet)
            if packet["method"] == "Runtime.evaluate":
                if packet["params"]["expression"] != "0":
                    entered.set()
                await asyncio.Future()

        async def request():
            await execute_script(
                SimpleNamespace(send=send), "while(true){}", None, format="script"
            )

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(request)
            await entered.wait()
            tasks.cancel_scope.cancel()
        methods = [packet["method"] for packet in commands]
        self.assertIn("Runtime.terminateExecution", methods)
        self.assertIn("Runtime.releaseObjectGroup", methods)

    async def test_native_evaluation_cancellation_interrupts_busy_js_only(self):
        for busy in (False, True):
            commands = []
            entered = asyncio.Event()

            async def send(command):
                packet = next(command)
                commands.append(packet)
                if packet["method"] == "Runtime.evaluate":
                    if packet["params"]["expression"] != "0":
                        entered.set()
                        await asyncio.Future()
                    if busy:
                        await asyncio.Future()
                    return uc.cdp.runtime.RemoteObject(type_="number", value=0), None

            with self.subTest(busy=busy):
                task = asyncio.create_task(
                    execute_script(
                        SimpleNamespace(send=send),
                        "while(true){}" if busy else "new Promise(() => {})",
                        None,
                        format="script",
                    )
                )
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                methods = [packet["method"] for packet in commands]
                self.assertEqual(methods.count("Runtime.terminateExecution"), int(busy))
                self.assertEqual(methods[-1], "Runtime.releaseObjectGroup")

    async def test_argument_cancellation_stops_synchronous_js_but_not_idle_promises(
        self,
    ):
        for promise in (False, True):
            commands = []
            entered = asyncio.Event()

            async def send(command):
                packet = next(command)
                commands.append(packet)
                method = packet["method"]
                if method == "Runtime.callFunctionOn" and promise:
                    return uc.cdp.runtime.RemoteObject(
                        type_="object",
                        subtype="promise",
                        object_id=uc.cdp.runtime.RemoteObjectId("promise"),
                    ), None
                if method in {"Runtime.callFunctionOn", "Runtime.awaitPromise"}:
                    entered.set()
                    await asyncio.Future()
                if method == "Runtime.evaluate":
                    return uc.cdp.runtime.RemoteObject(type_="number", value=0), None

            with self.subTest(promise=promise):
                task = asyncio.create_task(
                    execute_script(
                        SimpleNamespace(send=send),
                        "el => 1",
                        ["uid"],
                        resolve_uid=AsyncMock(
                            return_value=SimpleNamespace(
                                object_id=uc.cdp.runtime.RemoteObjectId("element")
                            )
                        ),
                    )
                )
                await entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                methods = [p["method"] for p in commands]
                self.assertEqual(
                    methods.count("Runtime.terminateExecution"), int(not promise)
                )
                self.assertEqual(methods[-1], "Runtime.releaseObjectGroup")

    async def test_source_files_support_utf8_bom_file_urls_and_mutual_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "脚本 文件.js"
            path.write_text('() => "中文🙂"', encoding="utf-8-sig")
            for source_path in (str(path), path.as_uri()):
                args = {"source_path": source_path}
                await prepare_script_call(args)
                self.assertEqual(args["function"], '() => "中文🙂"')
                self.assertEqual(args["source_path"], "")
            for args in (
                {},
                {"function": "1", "source_path": str(path)},
                {"source_path": str(path / "missing")},
            ):
                with self.assertRaises(ValueError):
                    await prepare_script_call(args)
            path.write_bytes(b"\xff\xfe")
            with self.assertRaisesRegex(ValueError, "UTF-8"):
                await prepare_script_call({"source_path": str(path)})

    async def test_native_evaluation_has_deadline_and_releases_its_object_group(self):
        commands = []
        evaluations = 0

        async def send(command):
            nonlocal evaluations
            packet = next(command)
            commands.append(packet)
            if packet["method"] == "Runtime.evaluate":
                evaluations += 1
                return uc.cdp.runtime.RemoteObject(
                    type_="boolean", value=evaluations == 1
                ), None

        value = await execute_script(
            SimpleNamespace(send=send), "() => false", None, timeout=500
        )
        self.assertIs(value, False)
        evaluate = next(c for c in commands if c["method"] == "Runtime.evaluate")
        self.assertEqual(evaluate["params"]["timeout"], 500)
        self.assertEqual(commands[-1]["method"], "Runtime.releaseObjectGroup")
        self.assertEqual(
            commands[-1]["params"]["objectGroup"], evaluate["params"]["objectGroup"]
        )

    async def test_argument_resolution_failure_releases_already_created_handles(self):
        commands = []

        async def send(command):
            commands.append(next(command))

        resolve = AsyncMock(
            side_effect=[
                SimpleNamespace(object_id="first"),
                ValueError("stale second UID"),
            ]
        )
        with self.assertRaisesRegex(ValueError, "stale second"):
            await execute_script(
                SimpleNamespace(send=send),
                "(a,b) => 1",
                ["a", "b"],
                resolve_uid=resolve,
            )
        self.assertEqual(
            [c["method"] for c in commands], ["Runtime.releaseObjectGroup"]
        )
        self.assertEqual(
            resolve.call_args_list[0].args[1], resolve.call_args_list[1].args[1]
        )

    async def test_full_json_outputs_and_labeled_previews_survive_inline_limits(self):
        self.assertEqual(
            (await script_result(0)).splitlines()[:3], ["```json", "0", "```"]
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"NODRIVER_ARTIFACTS_DIR": directory}),
        ):
            value = {"rows": [{"i": i, "text": "中文🙂" * 10} for i in range(100)]}
            result = await script_result(value, max_output_chars=100)
            path = Path(json.loads(result.splitlines()[0].partition(": ")[2]))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), value)
            self.assertIn("Preview truncated", result)
            self.assertLess(len(result), 600)
            explicit = Path(directory) / "nested" / "用户.json"
            result = await script_result(False, str(explicit))
            self.assertEqual(json.loads(explicit.read_text(encoding="utf-8")), False)
            self.assertIn(str(explicit), result)

    async def test_non_json_values_are_reported_instead_of_silently_becoming_null(self):
        with self.assertRaisesRegex(ValueError, "bigint"):
            script_value(
                uc.cdp.runtime.RemoteObject(
                    type_="bigint",
                    unserializable_value=uc.cdp.runtime.UnserializableValue("123n"),
                )
            )
        self.assertEqual(
            script_value(
                uc.cdp.runtime.RemoteObject(
                    type_="number",
                    unserializable_value=uc.cdp.runtime.UnserializableValue("-0"),
                )
            ),
            0,
        )
        self.assertIsNone(
            script_value(
                uc.cdp.runtime.RemoteObject(
                    type_="number",
                    unserializable_value=uc.cdp.runtime.UnserializableValue("NaN"),
                )
            )
        )
