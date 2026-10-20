"""Opt-in regression test for fresh tool installation without the project lock."""

import asyncio
import os
import json
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
                    "--refresh-package",
                    "nodriver-mcp",
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
            performance_enabled = (
                os.environ.get("NODRIVER_MCP_PERFORMANCE_TESTS") == "1"
            )
            parameters = StdioServerParameters(
                command=str(executable),
                env={
                    **os.environ,
                    "NODRIVER_ENABLED_TOOL_GROUPS": "performance"
                    if performance_enabled
                    else "",
                },
            )
            async with stdio_client(parameters) as (
                reader,
                writer,
            ):
                async with ClientSession(reader, writer) as session:
                    await asyncio.wait_for(session.initialize(), timeout=30)
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    self.assertTrue(
                        {
                            "start_browser",
                            "close_browser",
                            "take_snapshot",
                        }
                        <= names
                    )
                    self.assertEqual(len(names), 67 if performance_enabled else 60)
                    self.assertNotIn("call_webmcp_tool", names)
                    self.assertNotIn("get_ad_info", names)
                    result = await session.call_tool(
                        "list_cdp_commands", {"domain": "ServiceWorker"}
                    )
                    self.assertFalse(result.is_error, str(result))

                    if performance_enabled:
                        output = root / "installed-analysis.json"
                        result = await session.call_tool(
                            "performance_analyze_trace",
                            {
                                "trace_path": str(
                                    project
                                    / "tests/fixtures/web-dev-with-commit.json.gz"
                                ),
                                "file_path": str(output),
                                "max_output_chars": 100,
                            },
                        )
                        self.assertFalse(result.is_error, str(result))
                        report = json.loads(output.read_text())
                        self.assertAlmostEqual(
                            report["web_vitals"][0]["lcp_ms"], 129.211
                        )
                        self.assertGreaterEqual(
                            len(report["insight_sets"][0]["insights"]), 19
                        )
