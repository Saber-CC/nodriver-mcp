import base64
import asyncio
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
    def parsed(self, maps, label, *, external=False):
        data = dict(
            mapping(),
            sources=[label + ".ts"],
            sourcesContent=["1\n2\n3\n4\nsource-" + label],
        )
        maps.parsed(
            NS(
                script_id="3",
                execution_context_id=1,
                hash_=label,
                execution_context_aux_data={"frameId": "root"},
                url=f"https://{label}.test/app.js",
                start_line=0,
                start_column=0,
                source_map_url="app.map"
                if external
                else "data:application/json;base64,"
                + base64.b64encode(json.dumps(data).encode()).decode(),
            )
        )

    async def test_reused_script_id_keeps_cached_and_uncached_old_messages_separate(
        self,
    ):
        tab = NS(send=AsyncMock(), handlers=defaultdict(list))
        maps = SourceMaps(tab)
        maps.active = maps.ever_enabled = True
        self.parsed(maps, "A")
        old = {
            "stack_trace": {
                "callFrames": [
                    {
                        "scriptId": "3",
                        "url": "https://A.test/app.js",
                        "lineNumber": 0,
                        "columnNumber": 0,
                    }
                ]
            }
        }
        maps.bind(old)
        self.assertIn("A.ts", (await maps.symbolize(old))["mapped_location"]["url"])
        maps.contexts_cleared(None)
        self.parsed(maps, "B")
        new = {
            "stack_trace": {
                "callFrames": [
                    {
                        "scriptId": "3",
                        "url": "https://B.test/app.js",
                        "lineNumber": 0,
                        "columnNumber": 0,
                    }
                ]
            }
        }
        maps.bind(new)
        self.assertIn("B.ts", (await maps.symbolize(new))["mapped_location"]["url"])
        self.assertIn("A.ts", (await maps.symbolize(old))["mapped_location"]["url"])
        # Replacing an ID without a contextsCleared notification also needs a
        # new cache key and a stable catalog for the already captured log.
        self.parsed(maps, "C")
        self.assertIn("B.ts", (await maps.symbolize(new))["mapped_location"]["url"])
        self.parsed(maps, "external", external=True)
        stale = {"location": {"script_id": "3", "line_number": 0, "column_number": 0}}
        maps.bind(stale)
        maps.contexts_cleared(None)
        self.parsed(maps, "D")
        result = await maps.symbolize(stale)
        self.assertIsNone(result.get("mapped_location"))
        self.assertIn("retired", result["source_map_errors"][0])
        tab.send.assert_not_awaited()

    def test_runtime_context_replay_completes_only_live_script_identities(self):
        maps = SourceMaps(NS(handlers=defaultdict(list)))
        self.parsed(maps, "A")
        script = maps.scripts["3"]
        maps.context_created(NS(context=NS(id_=1, unique_id="world-A")))
        self.assertEqual(script["context_unique_id"], "world-A")
        self.parsed(maps, "A")
        self.assertIs(maps.scripts["3"], script)
        self.assertFalse(script.get("retired"))

        maps.context_destroyed(
            NS(execution_context_id=1, execution_context_unique_id="world-A")
        )
        maps.context_created(NS(context=NS(id_=1, unique_id="world-B")))
        self.assertEqual(script["context_unique_id"], "world-A")
        self.parsed(maps, "A")
        self.assertIsNot(maps.scripts["3"], script)
        self.assertTrue(script["retired"])
        self.assertEqual(maps.scripts["3"]["context_unique_id"], "world-B")

        maps = SourceMaps(NS(handlers=defaultdict(list)))
        self.parsed(maps, "A")
        retired = maps.scripts["3"]
        maps.context_destroyed(
            NS(execution_context_id=1, execution_context_unique_id="unknown-old")
        )
        maps.context_created(NS(context=NS(id_=1, unique_id="new-world")))
        self.assertIsNone(retired["context_unique_id"])
        self.assertTrue(retired["retired"])

    async def test_inflight_old_map_load_cannot_populate_reused_script_cache(self):
        maps = SourceMaps(NS(handlers=defaultdict(list)))
        maps.active = maps.ever_enabled = True
        entered, release = asyncio.Event(), asyncio.Event()

        async def fetch(url, script):
            if script["hash"] == "A":
                entered.set()
                await release.wait()
            return dict(mapping(), sources=[script["hash"] + ".ts"])

        maps.fetch = fetch
        self.parsed(maps, "A", external=True)
        first = maps.scripts["3"]
        task = asyncio.create_task(maps.map_for(first))
        await entered.wait()
        self.parsed(maps, "B", external=True)
        second = maps.scripts["3"]
        release.set()
        self.assertIn("A.ts", (await task).find(0, 0)["url"])
        self.assertIn("B.ts", (await maps.map_for(second)).find(0, 0)["url"])
        self.assertNotEqual(first["identity"], second["identity"])

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
