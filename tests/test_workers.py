import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from nodriver_mcp.workers import WorkerTargets, worker_info


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_replaced_and_stopped_worker_connections_retire_without_page_fallback(
        self,
    ):
        def worker():
            return NS(
                target_id="worker",
                target=NS(
                    type_="service_worker", url="chrome-extension://extension-id/sw.js"
                ),
                aclose=AsyncMock(),
            )

        first, replacement = worker(), worker()
        retired = []
        registry = WorkerTargets(retired.append)
        page = NS(target_id="page", target=NS(type_="page", url="https://test"))
        browser = NS(targets=[page, first], update_targets=AsyncMock())
        self.assertIs(await registry.get(browser, "worker"), first)
        self.assertEqual(worker_info(first)["extension_id"], "extension-id")
        browser.targets = [page, replacement]
        self.assertIs(await registry.get(browser, "worker"), replacement)
        self.assertEqual(retired, [first])
        first.aclose.assert_awaited_once()
        browser.targets = [page]
        with self.assertRaisesRegex(ValueError, "stopped service_worker_id"):
            await registry.get(browser, "worker")
        replacement.aclose.assert_awaited_once()
        self.assertFalse(registry.connections)
