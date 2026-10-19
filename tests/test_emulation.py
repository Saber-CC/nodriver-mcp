import unittest
from unittest.mock import AsyncMock

from nodriver_mcp.emulation import EmulationOverrides, http_headers


class Connection:
    def __init__(self):
        self.commands = []

        async def send(command):
            self.commands.append(next(command))

        self.send = AsyncMock(side_effect=send)


class EmulationTests(unittest.IsolatedAsyncioTestCase):
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
