# Official nodriver 0.50.6 integration

The baseline is the patched 0.48.1 build at `ec323db`. The target is the
official 0.50.6 release (`219a60d`). The package and lock file pin the PyPI
release; a checkout next to this project is not required at runtime.

## Implementation plan

1. Adapt the browser/session hierarchy, callback removal, target discovery,
   selected pages, frame routing and cleanup to official 0.50.6.
2. Delegate cookies to the official `CookieJar` (`get_all`, `set_all`,
   `save`, `load`, `clear`). Preserve the existing JSON session format while
   restoring cookies in a typed batch. Scope cookies to the selected browser
   context. Native cookie files use nodriver's pickle format and are intended
   for files created locally by `save_cookies`.
3. Delegate ordinary PNG/JPEG screenshots to `Tab.save_screenshot`, using
   `as_base64=True` for image responses. Keep official CDP calls for element
   crops, WebP and quality options which the high-level helper does not expose.
4. Expose native frame discovery, text/CSS/XPath search, connection history,
   browser information, WebMCP discovery/invocation/cancellation, page search,
   screen recording, annotated content, durable network capture and extension
   discovery/actions.
5. Expose every official generated command and event through typed discovery,
   invocation and subscriptions. This includes all new protocol domains without
   requiring hundreds of individual MCP tools. Parameters are converted with
   nodriver's own generated types and serializers; no custom CDP transport is
   introduced. Discovery describes the installed bindings, not a guarantee that
   a particular Chrome build implements every experimental command.
6. Repair existing form return handling, request IDs/filtering and trace stream
   consumption. Add behavioral regression tests and local Chrome integration
   checks using temporary profiles and local pages.

## Compatibility details

- Use `Browser.send`; the former `browser.connection` is no longer the sender.
- Create page targets rather than `for_tab=True` wrapper targets. Refresh the
  official target inventory while waiting for new pages.
- Keep the original callback when removing a handler. Do not automatically
  enable `Runtime` for console collection.
- Track selected pages explicitly. Keep request and console records with their
  originating page; snapshot UIDs also retain their connection and frame.
- Consume `TracingComplete.stream` with official `IO.read`/`IO.close`.
- `Tab.get()` in 0.50.6 reattaches a second session after navigation, leaving
  Network enabled on the old session. Existing-session navigation therefore
  uses the official `Page.navigate` command, preserving event/body ownership.
- `CookieJar` does not pass a browser context ID itself. A narrow scope adapter
  supplies it to the official generated Storage commands for isolated contexts.
- Script evaluation uses official `Runtime.evaluate` with JSON serialization;
  the high-level helper's deep object graph is unsuitable for MCP JSON results.
- Official 0.50.6 removes `sameParty` and uses
  `localNetworkAccessRequestPolicy`; the fork-specific Chrome 146 patch is no
  longer required.

## Coverage policy

The command inventory below was derived from the old installed package and the
official source, rather than release-note titles. Common operations have named
tools; every listed command is also available through `send_cdp_command`.
Event-driven domains such as SmartCardEmulation use `subscribe_cdp_events`,
`get_cdp_events` and `unsubscribe_cdp_events` together with their report commands.

Native `CookieJar.save` does not correctly apply its pattern filter in 0.50.6,
so `save_cookies` intentionally saves the whole selected context. Its companion
`load_cookies` supports the official load-time pattern filter.

## Added protocol commands

| Domain | Added commands |
| --- | --- |
| ads | `get_ad_metrics`, `get_ad_scripts` |
| browser | `add_mock_camera`, `get_global_privacy_control`, `set_global_privacy_control` |
| crash_report_context | `get_entries` |
| css | `force_position_try_option`, `set_container_query_condition_text`, `set_navigation_text` |
| digital_credentials | `set_virtual_wallet_behavior` |
| dom | `clear_text_markers`, `force_show_interest`, `get_implicit_anchor_candidates`, `set_text_marker` |
| emulation | `set_cpu_performance_override`, `set_primary_screen`, `set_virtual_keyboard_geometry_override`, `update_screen` |
| extensions | `get_extensions`, `trigger_action` |
| find_in_page | `find_first`, `find_next`, `find_prev`, `stop` |
| network | `configure_durable_messages`, `delete_device_bound_session`, `enable_device_bound_sessions`, `fetch_schemeful_site` |
| overlay | `set_show_display_cutout`, `set_show_inspected_element_anchor` |
| page | `get_annotated_page_content`, `get_sibling_sub_apps`, `get_sub_apps`, `start_screen_recording`, `stop_screen_recording` |
| smart_card_emulation | `disable`, `enable`, `report_begin_transaction_result`, `report_connect_result`, `report_data_result`, `report_error`, `report_establish_context_result`, `report_get_status_change_result`, `report_list_readers_result`, `report_plain_result`, `report_release_context_result`, `report_status_result` |
| storage | `clear_private_verification_tokens`, `delete_private_verification_token`, `get_private_verification_tokens`, `get_private_verification_tokens_issuer_configs`, `set_private_verification_tokens_tracking` |
| target | `get_dev_tools_target` |
| tracing | `get_track_event_descriptor` |
| web_mcp | `cancel_invocation`, `disable`, `enable`, `invoke_tool` |

Total: **58** added generated commands.

## Validation results

Verified locally on 2026-10-19 against official PyPI nodriver 0.50.6 and
Chrome 154, with temporary headless browser profiles and a loopback HTTP server.

- 29 regression/integration tests: **28 passed, 1 skipped**.
- Native element input (including Unicode), CSS/XPath/best-match search,
  same-process frame selection and scoped operations passed.
- Native CookieJar save/load/clear, isolated cookie contexts, batch expiry
  conversion and JSON session/localStorage round trips passed.
- Native image responses passed both direct calls and MCP content conversion;
  full-page output was checked against a 2400-pixel document.
- Network filtering/body retrieval, explicit console cleanup and nonempty trace
  stream export passed.
- A real stdio MCP client initialized, discovered tools and called command
  discovery successfully. All 675 command annotations resolved without errors.
- Ruff F checks, lock validation, whitespace checks and source/wheel builds passed.

The WebMCP page-registration test was skipped because this Chrome build did not
expose `navigator.modelContext`, including when experimental switches were tried.
The CDP domain itself can be enabled; registration and invocation event handling
are covered by a deterministic regression test. This is not an end-to-end website
WebMCP validation. Hardware-dependent/experimental commands are accessible through
the official bindings but were not all executed on physical devices.

For same-process frames, script evaluation uses an isolated execution world;
DOM access is scoped to that frame, while page-defined JavaScript globals may
require an explicit main-world context through the CDP tools.

The native cookie file helpers emit a ResourceWarning under CPython's resource
checks because upstream opens files without a `with` block. They remain delegated
to the official implementation; the round-trip tests pass. No nodriver source
files are patched or vendored by this project.
