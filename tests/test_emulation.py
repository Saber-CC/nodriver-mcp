import unittest
import asyncio
from collections import defaultdict
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from nodriver import cdp

from nodriver_mcp.emulation import EmulationOverrides, FrameOverrides, http_headers


class Connection:
    def __init__(self):
        self.commands = []

        async def send(command):
            self.commands.append(next(command))

        self.send = AsyncMock(side_effect=send)


class EmulationTests(unittest.IsolatedAsyncioTestCase):
    async def test_frame_guard_configures_native_child_session_before_resume_and_releases_it(
        self,
    ):
        for failing in (False, True):
            commands, handlers = [], defaultdict(list)
            enable_collection = AsyncMock()
            manager = EmulationOverrides(enable_collection)
            manager.states["a"] = {
                "media": {"prefers-reduced-motion": "reduce"},
                "headers": {"X-Review": "first"},
            }

            async def send(command, **routing):
                packet = next(command)
                commands.append(dict(packet, **routing))
                if failing and packet["method"] == "Network.setExtraHTTPHeaders":
                    raise ValueError("detached during setup")

            guard = NS(
                send=send,
                aclose=AsyncMock(),
                add_handler=lambda event, callback: handlers[event].append(callback),
            )
            frames = FrameOverrides(manager)
            with patch("nodriver_mcp.emulation.Tab", return_value=guard):
                await frames.watch(NS(target_id="a", target=NS()))
            handlers[cdp.target.AttachedToTarget][0](
                NS(session_id="child-session", target_info=NS(type_="iframe"))
            )
            await asyncio.gather(*frames.pages["a"]["tasks"])
            child = [p for p in commands if p.get("sessionId") == "child-session"]
            methods = [p["method"] for p in child]
            self.assertLess(
                methods.index("Network.setExtraHTTPHeaders"),
                methods.index("Runtime.runIfWaitingForDebugger"),
            )
            self.assertEqual(methods[-1], "Runtime.runIfWaitingForDebugger")
            self.assertNotIn("Runtime.enable", methods)
            enable_collection.assert_not_awaited()
            if not failing:
                await manager.update(
                    "a", frames.connections("a"), headers={"X-Review": "next"}
                )
                self.assertEqual(
                    commands[-1]["params"]["headers"], {"X-Review": "next"}
                )
                enable_collection.assert_not_awaited()
            await frames.close_all()
            guard.aclose.assert_awaited_once()
            self.assertFalse(frames.pages)
            self.assertFalse(manager.applied)

    async def test_media_merge_reset_and_new_frame_sync_are_isolated_and_retryable(
        self,
    ):
        manager = EmulationOverrides()
        a, b, frame = Connection(), Connection(), Connection()
        await manager.update(
            "a", [a], {"prefers-color-scheme": "dark"}, {"X-Owner": "a"}
        )
        await manager.update("b", [b], {"forced-colors": "active"}, {"X-Owner": "b"})
        await manager.update("a", [a], {"prefers-reduced-motion": "reduce"})
        await manager.update("a", [a], {"prefers-color-scheme": None})
        self.assertEqual(
            manager.states["a"]["media"], {"prefers-reduced-motion": "reduce"}
        )
        frame.send.side_effect = ValueError("temporarily detached")
        with self.assertRaisesRegex(ValueError, "detached"):
            await manager.sync(frame, "a")
        self.assertNotIn(id(frame), manager.applied)
        frame.send = AsyncMock(
            side_effect=lambda command: frame.commands.append(next(command))
        )
        await manager.sync(frame, "a")
        self.assertEqual(frame.commands[-1]["params"]["headers"], {"X-Owner": "a"})
        before = len(frame.commands)
        await manager.sync(frame, "a")
        self.assertEqual(len(frame.commands), before)
        await manager.update("a", [a, frame], reset=True)
        self.assertEqual(frame.commands[-1]["params"]["headers"], {})
        self.assertEqual(manager.states["b"]["headers"], {"X-Owner": "b"})
        manager.retire(frame)
        self.assertNotIn(id(frame), manager.applied)
        manager.forget("a")
        self.assertNotIn("a", manager.states)

    async def test_headers_accept_objects_and_json_and_reject_malformed_values(self):
        self.assertEqual(http_headers('{"X-Test":"value"}'), {"X-Test": "value"})
        self.assertEqual(http_headers(""), {})
        self.assertIsNone(http_headers(None))
        for invalid in (
            [],
            "null",
            "{bad",
            {"invalid:name": "v"},
            {"X": "a\r\nb"},
            {"X": 4},
        ):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                http_headers(invalid)
