"""Opt-in regression test for fresh tool installation without the project lock."""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@unittest.skipUnless(
    os.environ.get("NODRIVER_MCP_INSTALL_TESTS") == "1",
    "Fresh dependency resolution is opt-in",
)
@unittest.skipUnless(shutil.which("uv"), "uv is required for tool installation")
class InstallationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_tool_install_starts_and_exposes_browser_tools(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="nodriver-mcp-install-") as directory:
            root = Path(directory)
            installed = await asyncio.to_thread(
                subprocess.run,
                [
                    shutil.which("uv"),
                    "tool",
                    "install",
                    "--python",
                    sys.executable,
                    str(project),
                ],
                env={
                    **os.environ,
                    "UV_TOOL_DIR": str(root / "tools"),
                    "UV_TOOL_BIN_DIR": str(root / "bin"),
                },
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(
                installed.returncode, 0, installed.stdout + installed.stderr
            )
            executable = (
                root
                / "bin"
                / ("nodriver-mcp.exe" if os.name == "nt" else "nodriver-mcp")
            )
            async with stdio_client(StdioServerParameters(command=str(executable))) as (
                reader,
                writer,
            ):
                async with ClientSession(reader, writer) as session:
                    await asyncio.wait_for(session.initialize(), timeout=30)
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    self.assertTrue(
                        {"start_browser", "close_browser", "take_snapshot"} <= names
                    )
                    result = await session.call_tool(
                        "list_cdp_commands", {"domain": "ServiceWorker"}
                    )
                    self.assertFalse(result.is_error, str(result))
