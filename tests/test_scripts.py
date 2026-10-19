import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nodriver as uc
from nodriver_mcp.scripts import execute_script, script_result, script_value


class ScriptTests(unittest.IsolatedAsyncioTestCase):
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
