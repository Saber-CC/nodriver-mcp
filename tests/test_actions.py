import asyncio
import collections
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import anyio
from nodriver import cdp
from nodriver_mcp.actions import ActionInterrupted, ActionWaiter


class Connection:
    def __init__(self, target="root"):
        self.target_id = target
        self.handlers = collections.defaultdict(list)
        self.send = AsyncMock()
        self.get_frame_tree = AsyncMock(
            return_value=SimpleNamespace(frame=SimpleNamespace(id_=target))
        )

    def add_handler(self, event, callback):
        self.handlers[event].append(callback)

    def remove_handler(self, event, callback):
        self.handlers[event].remove(callback)

    def emit(self, kind, event):
        for callback in self.handlers[kind][:]:
            callback(event)


class ActionTests(unittest.IsolatedAsyncioTestCase):
    def waiter(self, **options):
        self.page, self.browser = Connection(), Connection("browser")
        self.evaluate = AsyncMock(return_value="stable")
        self.navigated = Mock()
        return ActionWaiter(
            self.page,
            self.page,
            self.browser,
            self.evaluate,
            self.navigated,
            probe=0.002,
            navigation_timeout=0.02,
            dom_timeout=0.02,
            **options,
        )

    async def test_observes_before_action_and_waits_for_matching_navigation(self):
        waiter = self.waiter()
        external = Mock()
        self.page.add_handler(cdp.page.FrameNavigated, external)

        async def action():
            self.assertEqual(len(self.page.handlers[cdp.page.FrameNavigated]), 2)
            self.page.emit(
                cdp.page.FrameStartedNavigating,
                cdp.page.FrameStartedNavigating(
                    cdp.page.FrameId("root"),
                    "https://done",
                    cdp.network.LoaderId("new"),
                    "differentDocument",
                ),
            )
            self.page.emit(
                cdp.page.FrameNavigated,
                SimpleNamespace(frame=SimpleNamespace(id_="root", url="https://done")),
            )
            asyncio.get_running_loop().call_later(
                0.005,
                self.page.emit,
                cdp.page.FrameStoppedLoading,
                cdp.page.FrameStoppedLoading(cdp.page.FrameId("root")),
            )
            self.browser.emit(
                cdp.target.TargetCreated,
                SimpleNamespace(
                    target_info=SimpleNamespace(
                        opener_id="root", type_="page", target_id="popup"
                    )
                ),
            )
            return False

        self.assertIs(await waiter.run(action), False)
        self.assertTrue(waiter.loaded.is_set())
        self.navigated.assert_called_once_with("root")
        self.assertEqual(waiter.result.url, "https://done")
        self.assertEqual(waiter.result.opened_pages, ["popup"])
        self.assertEqual(self.page.handlers[cdp.page.FrameNavigated], [external])
        self.assertFalse(waiter.callbacks)

    async def test_dialog_interrupts_pending_action_and_removes_handlers(self):
        waiter = self.waiter()
        cancelled = asyncio.Event()

        async def action():
            self.page.emit(
                cdp.page.JavascriptDialogOpening,
                SimpleNamespace(type_="alert", message="probe"),
            )
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        with self.assertRaisesRegex(ActionInterrupted, "handle_dialog"):
            await waiter.run(action)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(any(self.page.handlers.values()))
        self.assertFalse(any(self.browser.handlers.values()))

    async def test_cancellation_cleans_pending_action_and_observers(self):
        waiter = self.waiter()
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def action():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleaned.set()

        task = asyncio.create_task(waiter.run(action))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cleaned.is_set())
        self.assertTrue(waiter.dialog.cancelled())
        self.assertFalse(any(self.page.handlers.values()))

    async def test_anyio_cancellation_does_not_interrupt_child_cleanup(self):
        waiter = self.waiter(wait_for_stable=False)
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def action():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                with anyio.move_on_after(0.5, shield=True):
                    await asyncio.sleep(0.02)
                    cleaned.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(waiter.run, action)
            await entered.wait()
            tasks.cancel_scope.cancel()
        self.assertTrue(cleaned.is_set())
        self.assertTrue(waiter.dialog.cancelled())
        self.assertFalse(any(self.page.handlers.values()))

    async def test_stalled_dom_setup_is_bounded_and_attempts_cleanup(self):
        waiter = self.waiter()

        async def evaluate(_tab, expression):
            if "MutationObserver" in expression:
                await asyncio.Future()

        self.evaluate.side_effect = evaluate
        await asyncio.wait_for(waiter.run(AsyncMock(return_value=42)), 1)
        self.assertIn("could not be confirmed", waiter.result.summary())
        self.assertIn("?.()", self.evaluate.call_args.args[1])
        self.assertFalse(any(self.page.handlers.values()))

    async def test_read_only_mode_skips_dom_and_setup_errors_keep_original_failure(
        self,
    ):
        waiter = self.waiter(wait_for_stable=False)
        self.assertEqual(await waiter.run(AsyncMock(return_value=0)), 0)
        self.evaluate.assert_not_awaited()
        waiter = self.waiter()
        error = ValueError("setup failed")
        self.page.send.side_effect = error
        with self.assertRaises(ValueError) as caught:
            await waiter.run(AsyncMock())
        self.assertIs(caught.exception, error)
        self.assertFalse(any(self.page.handlers.values()))
