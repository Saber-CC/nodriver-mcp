"""Running service-worker targets, isolated from selected page and frame state."""

import asyncio
import logging
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


class WorkerTargets:
    def __init__(self, retire):
        self.retire = retire
        self.clear()

    def clear(self):
        self.connections = {}
        self.lock = asyncio.Lock()

    async def discover(self, browser):
        async with self.lock:
            await browser.update_targets()
            current = {
                str(tab.target_id): tab
                for tab in browser.targets
                if tab.target.type_ == "service_worker"
            }
            for key, connection in list(self.connections.items()):
                if current.get(key) is not connection:
                    self.connections.pop(key, None)
                    self.retire(connection)
                    try:
                        await connection.aclose()
                    except Exception:
                        logger.debug("Worker connection cleanup failed", exc_info=True)
            self.connections.update(current)
            return list(current.values())

    async def get(self, browser, worker_id):
        workers = await self.discover(browser)
        for worker in workers:
            if str(worker.target_id) == worker_id:
                return worker
        raise ValueError(
            f"Unknown or stopped service_worker_id {worker_id!r}. Use list_pages(include_service_workers=true)."
        )


def worker_info(worker):
    url = worker.target.url
    parsed = urlsplit(url)
    return {
        "service_worker_id": str(worker.target_id),
        "url": url,
        "extension_id": parsed.netloc if parsed.scheme == "chrome-extension" else None,
    }
