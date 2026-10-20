import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anyio
import nodriver as uc

from nodriver_mcp.advanced import _browser_version_page, _extension_tab_target


class VersionPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.commands = []

        async def send(command):
            packet = next(command)
            self.commands.append(packet)
            if packet["method"] == "Target.createTarget":
                return uc.cdp.target.TargetID("version")
            if packet["method"] == "Target.getTargets":
                return []
            return True

        self.browser = SimpleNamespace(send=AsyncMock(side_effect=send))
        self.tab = SimpleNamespace(send=AsyncMock(), aclose=AsyncMock())
        tab_patch = patch("nodriver_mcp.advanced.uc.Tab", return_value=self.tab)
        tab_patch.start()
        self.addCleanup(tab_patch.stop)

    def assert_closed(self):
        self.assertIn(
            {"method": "Target.closeTarget", "params": {"targetId": "version"}},
            self.commands,
        )

    async def test_waits_for_rows_and_closes_background_page(self):
        expected = {"Google Chrome": "154.0", "Command Line": "chrome --test"}
        self.tab.send.side_effect = [
            (uc.cdp.runtime.RemoteObject(type_="object", value={}), None),
            (uc.cdp.runtime.RemoteObject(type_="object", value=expected), None),
        ]
        self.assertEqual(await _browser_version_page(self.browser), expected)
        self.assertTrue(self.commands[0]["params"]["background"])
        self.assert_closed()
        self.tab.aclose.assert_awaited_once()

    async def test_read_failure_still_closes_page_and_connection(self):
        self.tab.send.side_effect = uc.ProtocolException(
            {"code": -32000, "message": "Version page failed"}
        )
        with self.assertRaisesRegex(uc.ProtocolException, "Version page failed"):
            await _browser_version_page(self.browser)
        self.assert_closed()
        self.tab.aclose.assert_awaited_once()

    async def test_cancellation_during_read_still_closes_page(self):
        entered = asyncio.Event()

        async def read(_command):
            entered.set()
            await asyncio.Future()

        self.tab.send.side_effect = read
        task = asyncio.create_task(_browser_version_page(self.browser))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_closed()
        self.tab.aclose.assert_awaited_once()

    async def test_cancellation_during_creation_keeps_reply_for_cleanup(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(command):
            packet = next(command)
            if packet["method"] == "Target.createTarget":
                entered.set()
                await release.wait()
                self.commands.append(packet)
                return uc.cdp.target.TargetID("version")
            self.commands.append(packet)
            if packet["method"] == "Target.getTargets":
                return []
            return True

        self.browser.send.side_effect = send
        task = asyncio.create_task(_browser_version_page(self.browser))
        await entered.wait()
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_closed()
        self.tab.send.assert_not_awaited()

    async def test_anyio_cancellation_waits_for_creation_before_closing(self):
        entered, release = asyncio.Event(), asyncio.Event()
        scopes = []

        async def send(command):
            packet = next(command)
            self.commands.append(packet)
            if packet["method"] == "Target.createTarget":
                entered.set()
                await release.wait()
                return uc.cdp.target.TargetID("version")
            return [] if packet["method"] == "Target.getTargets" else True

        async def request():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await _browser_version_page(self.browser)

        self.browser.send.side_effect = send
        task = asyncio.create_task(request())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            scopes[0].cancel()
            await asyncio.sleep(0.02)
            release.set()
            await asyncio.wait_for(task, 1)
            self.assert_closed()
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_cancelled_creation_without_a_reply_has_bounded_cleanup(self):
        entered, reaped = asyncio.Event(), asyncio.Event()

        async def send(_command):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                reaped.set()

        self.browser.send.side_effect = send
        task = asyncio.create_task(_browser_version_page(self.browser))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaisesRegex(TimeoutError, "did not close within 5000ms"):
            await asyncio.wait_for(task, 7)
        self.assertTrue(reaped.is_set())


class ExtensionTargetTests(unittest.IsolatedAsyncioTestCase):
    async def test_identical_urls_resolve_by_renderer_and_release_sessions(self):
        page = SimpleNamespace(
            target_id=uc.cdp.target.TargetID("second-page"),
            target=SimpleNamespace(browser_context_id="default"),
        )
        targets = [
            SimpleNamespace(
                target_id=uc.cdp.target.TargetID(name),
                browser_context_id="default",
                url="https://identical.test/",
                title="Identical",
            )
            for name in ("first-tab", "second-tab")
        ]
        browser = SimpleNamespace(send=AsyncMock(return_value=targets))
        guards = []

        def guard(target, **_kwargs):
            callbacks = []

            async def send(command):
                self.assertFalse(next(command)["params"]["waitForDebuggerOnStart"])
                event = SimpleNamespace(
                    target_info=SimpleNamespace(
                        target_id="first-page"
                        if target.target_id == "first-tab"
                        else "second-page"
                    )
                )
                for callback in callbacks:
                    callback(event)

            result = SimpleNamespace(
                add_handler=lambda _event, callback: callbacks.append(callback),
                send=AsyncMock(side_effect=send),
                aclose=AsyncMock(),
            )
            guards.append(result)
            return result

        with patch("nodriver_mcp.advanced.uc.Tab", side_effect=guard):
            self.assertEqual(await _extension_tab_target(browser, page), "second-tab")
        self.assertEqual(len(guards), 2)
        for item in guards:
            item.aclose.assert_awaited_once()

    async def test_absent_owner_fails_explicitly(self):
        browser = SimpleNamespace(send=AsyncMock(return_value=[]))
        with self.assertRaisesRegex(ValueError, "No owning tab"):
            await _extension_tab_target(browser, SimpleNamespace())

    async def test_failed_discovery_releases_temporary_connection(self):
        browser = SimpleNamespace(
            send=AsyncMock(return_value=[SimpleNamespace(browser_context_id="default")])
        )
        page = SimpleNamespace(target=SimpleNamespace(browser_context_id="default"))
        guard = SimpleNamespace(
            add_handler=lambda *_args: None,
            send=AsyncMock(side_effect=RuntimeError("discovery failed")),
            aclose=AsyncMock(),
        )
        with patch("nodriver_mcp.advanced.uc.Tab", return_value=guard):
            with self.assertRaisesRegex(RuntimeError, "discovery failed"):
                await _extension_tab_target(browser, page)
        guard.aclose.assert_awaited_once()
