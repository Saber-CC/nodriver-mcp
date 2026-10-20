"""Optional MCP tool groups, hidden by default and enabled at server startup."""

ENABLED_GROUPS_ENV = "NODRIVER_ENABLED_TOOL_GROUPS"

OPTIONAL_TOOL_GROUPS = {
    "performance": frozenset(
        {
            "performance_start_trace",
            "performance_stop_trace",
            "performance_analyze_trace",
            "performance_analyze_insight",
            "take_memory_snapshot",
            "start_screen_recording",
            "stop_screen_recording",
        }
    ),
    "diagnostics": frozenset(
        {
            "get_ad_info",
            "get_crash_report_context",
            "get_annotated_page_content",
            "get_connection_history",
        }
    ),
    "webmcp": frozenset(
        {
            "list_webmcp_tools",
            "call_webmcp_tool",
            "get_webmcp_invocation",
            "cancel_webmcp_invocation",
        }
    ),
}

TOOL_GROUP_BY_NAME = {
    name: group for group, names in OPTIONAL_TOOL_GROUPS.items() for name in names
}


def parse_enabled_tool_groups(value: str) -> frozenset[str]:
    groups = {part.strip().lower() for part in value.split(",") if part.strip()}
    unknown = groups - OPTIONAL_TOOL_GROUPS.keys() - {"all"}
    if unknown:
        raise ValueError(
            f"Unknown {ENABLED_GROUPS_ENV} group(s): {', '.join(sorted(unknown))}. "
            "Choose performance, diagnostics, webmcp or all."
        )
    return frozenset(OPTIONAL_TOOL_GROUPS) if "all" in groups else frozenset(groups)
