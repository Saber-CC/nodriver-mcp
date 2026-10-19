import base64
import json
import unittest
from collections import defaultdict
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from nodriver import cdp

from nodriver_mcp.source_maps import SourceMap, SourceMaps


def mapping(**overrides):
    return dict(
        version=3,
        sources=["original.ts"],
        names=["originalLogger"],
        mappings="AAIEA",
        sourceRoot="/src/",
        sourcesContent=["1\n2\n3\n4\n  console.error('original');"],
        **overrides,
    )


class SourceMapTests(unittest.IsolatedAsyncioTestCase):
    def test_vlq_root_names_unmapped_segments_and_indexed_offsets(self):
        basic = mapping()
        source = SourceMap(basic, "https://example.test/build/app.js.map")
        location = source.find(0, 50)
        self.assertEqual(location["url"], "https://example.test/src/original.ts")
        self.assertEqual((location["lineNumber"], location["columnNumber"]), (4, 2))
        self.assertEqual(location["functionName"], "originalLogger")
        self.assertIn("console.error", location["sourceLine"])
        unmapped = dict(basic, mappings="AAIEA,K")
        self.assertIsNone(SourceMap(unmapped, "https://test/app.map").find(0, 5))
        indexed = {
            "version": 3,
            "sections": [{"offset": {"line": 2, "column": 10}, "map": basic}],
        }
        source = SourceMap(indexed, "https://test/app.map")
        self.assertIsNone(source.find(2, 9))
        self.assertEqual(source.find(2, 10)["lineNumber"], 4)
        self.assertEqual(
            SourceMap(
                dict(basic, sourceRoot="webpack:///"), "https://test/app.map"
            ).find(0, 0)["url"],
            "webpack:///original.ts",
        )
        for invalid in (
            dict(basic, mappings="g"),
            dict(basic, mappings="AAAAAA"),
            dict(basic, mappings="ACAA"),
            dict(basic, version=2),
        ):
            with self.assertRaises(ValueError):
                SourceMap(invalid, "https://test/map")

    async def test_inline_maps_and_async_parent_stacks_keep_generated_positions(self):
        tab = NS(send=AsyncMock(), handlers=defaultdict(list))
        maps = SourceMaps(tab)
        maps.active = maps.ever_enabled = True
        data = (
            "data:application/json;base64,"
            + base64.b64encode(json.dumps(mapping()).encode()).decode()
        )
        maps.scripts["1"] = {"id": "1", "url": "https://test/app.js", "map_url": data}
        frame = {
            "scriptId": "1",
            "url": "https://test/app.js",
            "lineNumber": 0,
            "columnNumber": 10,
            "functionName": "minified",
        }
        tab.send.return_value = NS(
            to_json=lambda: {"description": "setTimeout", "callFrames": [dict(frame)]}
        )
        original = {"stack_trace": {"callFrames": [frame], "parentId": {"id": "async"}}}
        result = await maps.symbolize(original)
        self.assertNotIn("originalLocation", frame)
        self.assertIn(
            "originalLocation", result["stack_trace"]["parent"]["callFrames"][0]
        )
        self.assertEqual(result["mapped_location"]["functionName"], "originalLogger")
        maps.scripts["1"].update(start_line=5, start_column=10)
        shifted = await maps.symbolize(
            {
                "stack_trace": {
                    "callFrames": [dict(frame, lineNumber=5, columnNumber=20)]
                }
            }
        )
        self.assertEqual(shifted["mapped_location"]["lineNumber"], 4)
        maps.scripts["1"].update(start_line=0, start_column=0)
        location_only = await maps.symbolize(
            {"location": {"script_id": "1", "line_number": 0, "column_number": 10}}
        )
        self.assertEqual(location_only["mapped_location"]["lineNumber"], 4)
        from nodriver_mcp.console import format_stack

        rendered = format_stack(
            {
                "callFrames": [
                    dict(
                        frame,
                        originalLocation={
                            "url": "original.ts",
                            "lineNumber": 4,
                            "columnNumber": 2,
                        },
                    )
                ]
            }
        )
        self.assertIn("minified", rendered)
        self.assertIn("original.ts:5:3", rendered)
        maps.retire()
        tab.send.reset_mock()
        self.assertIn(
            "originalLocation",
            (await maps.symbolize(original))["stack_trace"]["callFrames"][0],
        )
        tab.send.assert_not_awaited()

    async def test_browser_loader_uses_credentials_and_closes_stream_on_bad_map(self):
        commands = []

        async def send(command):
            packet = next(command)
            commands.append(packet)
            if packet["method"] == "Network.loadNetworkResource":
                return NS(
                    success=True,
                    http_status_code=200,
                    stream=cdp.io.StreamHandle("resource"),
                    net_error_name=None,
                )
            if packet["method"] == "IO.read":
                return False, "invalid JSON", True

        tab = NS(send=send, handlers=defaultdict(list))
        maps = SourceMaps(tab)
        maps.active = maps.ever_enabled = True
        maps.scripts["1"] = {
            "id": "1",
            "url": "https://test/app.js",
            "map_url": "app.map",
            "frame_id": "root",
        }
        result = await maps.symbolize(
            {
                "stack_trace": {
                    "callFrames": [
                        {"scriptId": "1", "lineNumber": 0, "columnNumber": 0}
                    ]
                }
            }
        )
        self.assertTrue(result["source_map_errors"])
        self.assertTrue(commands[0]["params"]["options"]["includeCredentials"])
        self.assertEqual(commands[0]["params"]["frameId"], "root")
        self.assertEqual(commands[-1]["method"], "IO.close")

    async def test_external_indexed_sections_and_percent_encoded_inline_maps(self):
        maps = SourceMaps(NS(handlers=defaultdict(list)))
        maps.fetch = AsyncMock(return_value=mapping())
        indexed = {
            "version": 3,
            "sections": [{"offset": {"line": 1, "column": 2}, "url": "part.map"}],
        }
        expanded = await maps.expand(indexed, "https://test/index.map", {"id": "1"})
        self.assertEqual(
            SourceMap(expanded, "https://test/index.map").find(1, 2)["lineNumber"], 4
        )
        maps.fetch.assert_awaited_once_with("https://test/part.map", {"id": "1"})
        from urllib.parse import quote

        maps = SourceMaps(NS(handlers=defaultdict(list)))
        self.assertEqual(
            await maps.fetch(
                "data:application/json," + quote(json.dumps(mapping())), {}
            ),
            mapping(),
        )
