import asyncio
import collections
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nodriver import cdp
from nodriver_mcp.uploads import FileUploads, upload_paths


class UploadTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, *, file=False, multiple=False):
        self.handlers = collections.defaultdict(list)
        self.commands = []

        async def send(command):
            self.commands.append(next(command))

        page = SimpleNamespace(
            send=send,
            add_handler=lambda kind, callback: self.handlers[kind].append(callback),
            remove_handler=lambda kind, callback: self.handlers[kind].remove(callback),
        )
        element = SimpleNamespace(
            tab=page,
            send_file=AsyncMock(),
            apply=AsyncMock(
                return_value={
                    "file": file,
                    "multiple": multiple,
                    "disabled": False,
                    "connected": True,
                }
            ),
        )
        return FileUploads(), page, element

    async def test_local_paths_validate_exclusivity_files_and_direct_native_upload(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "one.txt", Path(directory) / "two.txt"
            first.write_text("one")
            second.write_text("two")
            paths = upload_paths("", [str(first), str(second)])
            manager, page, element = self.fixture(file=True, multiple=True)
            await manager.upload(element, page, paths, AsyncMock(), None)
            element.send_file.assert_awaited_once_with(*paths)
            self.assertFalse(self.commands)
            self.assertFalse(manager.leases)
            for single, multiple in (
                (str(first), [str(second)]),
                ("", []),
                ("", [directory]),
                ("", [directory + "/missing"]),
            ):
                with self.assertRaises(ValueError):
                    upload_paths(single, multiple)
            manager, page, element = self.fixture(file=True)
            with self.assertRaisesRegex(ValueError, "multiple"):
                await manager.upload(element, page, paths, AsyncMock(), None)
            element.send_file.assert_not_awaited()

    async def test_button_arms_chooser_before_click_and_routes_to_its_frame(self):
        manager, page, element = self.fixture()
        frame = SimpleNamespace(send=AsyncMock())
        resolve = AsyncMock(return_value=frame)
        external = lambda event: None
        self.handlers[cdp.page.FileChooserOpened].append(external)

        async def click():
            self.assertEqual(self.commands[-1]["params"], {"enabled": True})
            event = cdp.page.FileChooserOpened(
                cdp.page.FrameId("child"), "selectMultiple", cdp.dom.BackendNodeId(7)
            )
            for callback in self.handlers[cdp.page.FileChooserOpened][:]:
                callback(event)

        await manager.upload(element, page, ["one", "two"], click, resolve)
        resolve.assert_awaited_once_with("child")
        command = next(frame.send.call_args.args[0])
        self.assertEqual(command["method"], "DOM.setFileInputFiles")
        self.assertEqual(
            command["params"], {"files": ["one", "two"], "backendNodeId": 7}
        )
        self.assertEqual(self.handlers[cdp.page.FileChooserOpened], [external])
        self.assertEqual(self.commands[-1]["params"], {"enabled": False})
        self.assertFalse(manager.leases)

    async def test_timeout_and_cancellation_disable_interception_and_remove_callbacks(
        self,
    ):
        for cancel in (False, True):
            manager, page, element = self.fixture()
            clicked = asyncio.Event()

            async def click():
                clicked.set()

            task = asyncio.create_task(
                manager.upload(
                    element,
                    page,
                    ["one"],
                    click,
                    None,
                    timeout_ms=10 if not cancel else 10000,
                )
            )
            await clicked.wait()
            if cancel:
                task.cancel()
            with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError):
                await task
            self.assertFalse(self.handlers[cdp.page.FileChooserOpened])
            self.assertEqual(self.commands[-1]["params"], {"enabled": False})
            self.assertFalse(manager.leases)

    async def test_non_multiple_chooser_fails_without_setting_files(self):
        manager, page, element = self.fixture()

        async def click():
            event = cdp.page.FileChooserOpened(
                cdp.page.FrameId("root"), "selectSingle", cdp.dom.BackendNodeId(7)
            )
            for callback in self.handlers[cdp.page.FileChooserOpened][:]:
                callback(event)

        with self.assertRaisesRegex(ValueError, "multiple"):
            await manager.upload(element, page, ["one", "two"], click, None)
        self.assertNotIn("DOM.setFileInputFiles", [c["method"] for c in self.commands])
        self.assertEqual(self.commands[-1]["params"], {"enabled": False})
