"""Native file uploads and scoped file-chooser interception."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from nodriver import cdp

logger = logging.getLogger(__name__)


def upload_paths(file_path: str, file_paths: list[str] | None) -> list[str]:
    if file_path and file_paths is not None:
        raise ValueError("Supply file_path or file_paths, not both.")
    paths = [file_path] if file_path else file_paths
    if not paths:
        raise ValueError("Supply file_path or a nonempty file_paths list.")
    result = []
    for value in paths:
        path = Path(value).expanduser()
        if not value or not path.is_file():
            raise ValueError(f"Upload file does not exist or is not a file: {value!r}.")
        result.append(str(path.resolve()))
    return result


class FileUploads:
    def __init__(self):
        self.leases = {}

    @asynccontextmanager
    async def lease(self, page):
        key = id(page)
        entry = self.leases.setdefault(key, {"lock": asyncio.Lock(), "users": 0})
        entry["users"] += 1
        try:
            async with entry["lock"]:
                yield
        finally:
            entry["users"] -= 1
            if entry["users"] == 0:
                self.leases.pop(key, None)

    async def upload(self, element, page, paths, click, resolve_frame, timeout_ms=3000):
        info = await element.apply("""el => ({
            file: el.tagName === 'INPUT' && el.type === 'file',
            multiple: !!el.multiple, disabled: el.matches(':disabled'), connected: el.isConnected
        })""")
        if not info["connected"] or info["disabled"]:
            raise ValueError("Upload element is detached or disabled.")
        async with self.lease(page):
            if info["file"]:
                if len(paths) > 1 and not info["multiple"]:
                    raise ValueError("This file input does not allow multiple files.")
                await element.send_file(*paths)
                return
            await self.choose(element, page, paths, click, resolve_frame, timeout_ms)

    async def choose(self, element, page, paths, click, resolve_frame, timeout_ms):
        future = asyncio.get_running_loop().create_future()
        callbacks, enabled = [], []
        try:
            async with asyncio.timeout(timeout_ms / 1000 if timeout_ms else None):
                for connection in {id(t): t for t in (page, element.tab)}.values():

                    def opened(event, _connection=None, owner=connection):
                        if not future.done():
                            future.set_result((owner, event))

                    connection.add_handler(cdp.page.FileChooserOpened, opened)
                    callbacks.append((connection, opened))
                    await connection.send(cdp.page.enable())
                    enabled.append(connection)
                    await connection.send(
                        cdp.page.set_intercept_file_chooser_dialog(True)
                    )
                await click()
                owner, event = await future
                if len(paths) > 1 and event.mode != "selectMultiple":
                    raise ValueError("This file chooser does not allow multiple files.")
                if event.backend_node_id is None:
                    raise ValueError(
                        "This file chooser does not expose a file input node."
                    )
                connection = (
                    await resolve_frame(str(event.frame_id)) if resolve_frame else owner
                )
                await connection.send(
                    cdp.dom.set_file_input_files(
                        paths,
                        backend_node_id=event.backend_node_id,
                    )
                )
        except TimeoutError as exc:
            raise TimeoutError(
                "Upload button did not open a file chooser within the configured timeout."
            ) from exc
        finally:
            for connection, callback in callbacks:
                connection.remove_handler(cdp.page.FileChooserOpened, callback)
            for connection in reversed(enabled):
                try:
                    await asyncio.wait_for(
                        connection.send(
                            cdp.page.set_intercept_file_chooser_dialog(False)
                        ),
                        1,
                    )
                except Exception:
                    logger.debug(
                        "File chooser interception cleanup failed", exc_info=True
                    )
            if not future.done():
                future.cancel()
