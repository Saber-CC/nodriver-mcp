import asyncio
import collections
import gc
import unittest
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nodriver import cdp
from nodriver_mcp.actions import ActionWaiter
from nodriver_mcp.dialogs import DialogInterrupted, DialogRegistry


class Connection:
    def __init__(self, target="page"):
        self.target_id = target
        self.handlers = collections.defaultdict(list)
        self.send = AsyncMock()
        self.get_frame_tree = AsyncMock(
            return_value=SimpleNamespace(frame=SimpleNamespace(id_=target))
        )

    def add_handler(self, kind, callback):
        self.handlers[kind].append(callback)

    def remove_handler(self, kind, callback):
        if callback in self.handlers[kind]:
            self.handlers[kind].remove(callback)

    def emit(self, kind, event):
        for callback in self.handlers[kind][:]:
            callback(event, self)


def event(message="same", kind="alert"):
    return SimpleNamespace(
        type_=kind, message=message, url="https://page", default_prompt="default"
    )


class DialogTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_session_events_and_repeated_identical_dialogs_have_distinct_lifetimes(
        self,
    ):
        registry = DialogRegistry()
        page, frame = Connection(), Connection("frame")
        registry.watch(page, "page")
        registry.watch(frame, "page")
        page.emit(cdp.page.JavascriptDialogOpening, event())
        first = registry.current["page"]
        frame.emit(cdp.page.JavascriptDialogOpening, event())
        self.assertIs(registry.current["page"], first)
        page.emit(cdp.page.JavascriptDialogClosed, object())
        frame.emit(cdp.page.JavascriptDialogOpening, event())
        second = registry.current["page"]
        self.assertNotEqual(first.id, second.id)
        page.emit(cdp.page.JavascriptDialogClosed, object())
        self.assertIs(registry.current["page"], second)
        frame.emit(cdp.page.JavascriptDialogClosed, object())
        self.assertFalse(registry.current)
        registry.clear()
        self.assertFalse(any(page.handlers.values()))
        self.assertFalse(any(frame.handlers.values()))

    async def test_handle_does_not_remove_next_dialog_and_passes_prompt_content(self):
        registry, page = DialogRegistry(), Connection()
        registry.watch(page, "page")
        page.emit(cdp.page.JavascriptDialogOpening, event(kind="prompt"))
        first = registry.current["page"]

        async def send(command):
            self.assertEqual(
                next(command)["params"], {"accept": True, "promptText": "answer"}
            )
            page.emit(cdp.page.JavascriptDialogClosed, object())
            page.emit(cdp.page.JavascriptDialogOpening, event(kind="prompt"))

        page.send.side_effect = send
        await registry.handle(first, "accept", "answer")
        self.assertTrue(first.closed)
        self.assertIsNot(registry.current["page"], first)
        registry.clear()

    async def test_watchers_preserve_external_handlers_and_release_collected_connections(
        self,
    ):
        registry, page = DialogRegistry(), Connection()
        external = lambda *_args: None
        page.add_handler(cdp.page.JavascriptDialogOpening, external)
        registry.watch(page, "page")
        registry.forget(page)
        self.assertEqual(page.handlers[cdp.page.JavascriptDialogOpening], [external])
        registry.watch(page, "page")
        page.emit(cdp.page.JavascriptDialogOpening, event())
        reference = weakref.ref(page)
        del page
        gc.collect()
        self.assertIsNone(reference())
        self.assertFalse(registry.watchers)
        self.assertFalse(registry.current)

    async def test_action_accepts_repeated_dialogs_and_cleans_its_private_registry(
        self,
    ):
        page, browser = Connection(), Connection("browser")
        waiter = ActionWaiter(
            page,
            page,
            browser,
            AsyncMock(return_value="stable"),
            lambda _id: None,
            dialog_action="accept",
            prompt_text="answer",
            probe=0.001,
        )
        closed = asyncio.Event()

        async def send(command):
            if next(command)["method"] == "Page.handleJavaScriptDialog":
                page.emit(cdp.page.JavascriptDialogClosed, object())
                closed.set()

        page.send.side_effect = send

        async def action():
            for _ in range(3):
                closed.clear()
                page.emit(cdp.page.JavascriptDialogOpening, event())
                await closed.wait()
            return "done"

        self.assertEqual(await asyncio.wait_for(waiter.run(action), 2), "done")
        self.assertEqual(len(waiter.result.dialogs), 3)
        self.assertTrue(all(record.closed for record in waiter.result.dialogs))
        self.assertFalse(any(page.handlers.values()))

    async def test_dialog_handling_failure_interrupts_instead_of_hanging(self):
        page, browser = Connection(), Connection("browser")
        waiter = ActionWaiter(
            page, page, browser, AsyncMock(), lambda _id: None, dialog_action="accept"
        )

        async def send(command):
            if next(command)["method"] == "Page.handleJavaScriptDialog":
                raise ValueError("handling refused")

        page.send.side_effect = send

        async def action():
            page.emit(cdp.page.JavascriptDialogOpening, event())
            await asyncio.Future()

        with self.assertRaisesRegex(DialogInterrupted, "handling refused"):
            await asyncio.wait_for(waiter.run(action), 2)
        self.assertFalse(any(page.handlers.values()))
