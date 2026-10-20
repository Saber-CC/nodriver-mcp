import os
import unittest
from unittest.mock import AsyncMock, patch

from mcp.server.mcpserver.exceptions import ToolError

from nodriver_mcp import server as s
from nodriver_mcp.tool_groups import ENABLED_GROUPS_ENV, parse_enabled_tool_groups


DISABLED_TOOLS = {
    "performance_start_trace",
    "performance_stop_trace",
    "performance_analyze_trace",
    "performance_analyze_insight",
    "take_memory_snapshot",
    "start_screen_recording",
    "stop_screen_recording",
    "get_ad_info",
    "get_crash_report_context",
    "get_annotated_page_content",
    "get_connection_history",
    "list_webmcp_tools",
    "call_webmcp_tool",
    "get_webmcp_invocation",
    "cancel_webmcp_invocation",
}


class ToolGroupConfigTests(unittest.TestCase):
    def test_empty_config_disables_optional_groups(self):
        self.assertEqual(parse_enabled_tool_groups(""), frozenset())
        self.assertEqual(parse_enabled_tool_groups(" ,  "), frozenset())

    def test_named_groups_allow_whitespace_case_and_duplicates(self):
        self.assertEqual(
            parse_enabled_tool_groups(" Performance, diagnostics,performance "),
            {"performance", "diagnostics"},
        )

    def test_all_enables_every_optional_group(self):
        self.assertEqual(
            parse_enabled_tool_groups("all"),
            {"performance", "diagnostics", "webmcp"},
        )

    def test_unknown_groups_fail_with_configuration_help(self):
        for value in ("trace", "all,unknown"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, ENABLED_GROUPS_ENV),
            ):
                parse_enabled_tool_groups(value)


class ToolGroupExposureTests(unittest.IsolatedAsyncioTestCase):
    def server(self, enabled=""):
        with patch.dict(os.environ, {ENABLED_GROUPS_ENV: enabled}):
            return s.BrowserMCPServer(
                "tool-groups-test", tools=s.mcp._tool_manager.list_tools()
            )

    async def test_default_hides_exactly_the_requested_fifteen_tools(self):
        server = self.server()
        visible = {tool.name for tool in await server.list_tools()}
        retained = {tool.name for tool in server._tool_manager.list_tools()}
        self.assertEqual(len(visible), 60)
        self.assertEqual(retained - visible, DISABLED_TOOLS)
        self.assertTrue(
            {
                "subscribe_cdp_events",
                "get_cdp_events",
                "unsubscribe_cdp_events",
                "save_session",
                "load_session",
                "list_sessions",
                "list_extensions",
                "trigger_extension_action",
            }
            <= visible
        )

    async def test_each_group_and_combination_can_be_enabled(self):
        for enabled, expected_count, included in (
            ("performance", 67, {"performance_start_trace", "stop_screen_recording"}),
            ("diagnostics", 64, {"get_connection_history", "get_ad_info"}),
            ("webmcp", 64, {"list_webmcp_tools", "call_webmcp_tool"}),
            ("performance,webmcp", 71, {"take_memory_snapshot", "call_webmcp_tool"}),
            ("all", 75, DISABLED_TOOLS),
        ):
            with self.subTest(enabled=enabled):
                visible = {
                    tool.name for tool in await self.server(enabled).list_tools()
                }
                self.assertEqual(len(visible), expected_count)
                self.assertTrue(included <= visible)

    async def test_disabled_cached_names_cannot_launch_or_use_the_browser(self):
        server = self.server()
        with patch.object(s, "_get_browser", AsyncMock()) as browser:
            for name in sorted(DISABLED_TOOLS):
                with (
                    self.subTest(name=name),
                    self.assertRaisesRegex(
                        ToolError, f"disabled.*{ENABLED_GROUPS_ENV}"
                    ),
                ):
                    await server.call_tool(name, {})
            browser.assert_not_awaited()

    async def test_enabled_tool_calls_reach_the_retained_implementation(self):
        server = self.server("diagnostics")

        async def probe() -> str:
            return "enabled diagnostic"

        server.remove_tool("get_ad_info")
        server.add_tool(probe, name="get_ad_info")
        result = await server.call_tool("get_ad_info", {})
        self.assertFalse(result.is_error)
        self.assertIn(
            "enabled diagnostic",
            "".join(getattr(item, "text", "") for item in result.content),
        )
