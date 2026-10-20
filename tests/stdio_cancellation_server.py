"""Real MCP server with timing barriers for cancellation integration tests."""

import asyncio
import sys
from pathlib import Path

import nodriver as uc

from nodriver_mcp import advanced, server


def main():
    directory = Path(sys.argv[1])
    native_execute = server.execute_script
    native_version = advanced._browser_version_page
    native_send = uc.Browser.send

    async def execute(*args, **kwargs):
        try:
            return await native_execute(*args, **kwargs)
        except asyncio.CancelledError:
            (directory / "script-cancelled").touch()
            raise

    async def send(self, command, *args, **kwargs):
        version = (
            command.gi_code.co_name == "create_target"
            and command.gi_frame.f_locals.get("url") == "chrome://version/"
        )
        result = await native_send(self, command, *args, **kwargs)
        if version:
            (directory / "version-created").write_text(str(result))
            while not (directory / "release-version").exists():
                await asyncio.sleep(0.005)
        return result

    async def version(browser):
        request = asyncio.current_task()

        async def observe():
            while not request.cancelling():
                await asyncio.sleep(0.005)
            (directory / "version-cancelled").touch()

        observer = asyncio.create_task(observe())
        try:
            return await native_version(browser)
        finally:
            if request.cancelling():
                (directory / "version-cancelled").touch()
            (directory / "version-finished").touch()
            observer.cancel()

    server.execute_script = execute
    advanced._browser_version_page = version
    uc.Browser.send = send
    server.main()


if __name__ == "__main__":
    main()
