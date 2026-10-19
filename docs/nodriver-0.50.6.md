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
- Page tools accept a per-call `page_id`. Stable Chrome target ID strings avoid
  list-index changes; numeric indexes remain compatible. A task-local context
  pins page/frame selection for nested tools and appended snapshots without
  changing the global default. Explicit closed IDs never fall back. Frame
  discovery locks, navigation history retention and screen recordings are scoped
  per page. Trace collection remains browser-wide and validates its page owner.
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
- Official 0.50.6 can omit recursive `or`/`not` fields while parsing
  `ServiceWorker.workerVersionUpdated.typedRouterRules`. Relevant MCP tool and
  event descriptions flag this limitation to prevent treating incomplete
  conditions as complete routing rules. These hints do not recover lost fields
  or change Chrome's routing. The dependency remains the official package.
- `Element.click()` uses JavaScript `el.click()`, so `click` and `click_at` share
  generated CDP mouse events to preserve focus, mouse events and trusted clicks.
  Double clicks send both press/release pairs with click counts 1 and 2;
  releases use `buttons=0`.
- `press_key` uses official `Input.dispatchKeyEvent` with modifier flags,
  physical codes, virtual key codes and text for ordinary input. The parser
  preserves literal plus keys such as `Control++` and rejects invalid chords
  before opening a page. Errors and cancellation trigger release attempts for
  all attempted keys, retaining the original error if a release also fails.
  macOS editing shortcuts include Chrome editing commands so `Meta+A` actually
  selects text. `type_text(submit_key=...)` uses the same dispatcher.
- `Element.clear_input()` and `send_keys("")` emit no input events. Empty fills
  use the native value setter through `Element.apply`, then dispatch bubbling
  `input` and `change` events, including for controlled input/textarea elements.
- Frame discovery refreshes connections when a frame moves between the page
  process and a separate target. Stable native connections are reused;
  replaced/removed documents invalidate UIDs, and obsolete iframe sockets close
  without closing the shared page. Selecting the root frame ID selects the main
  page, which has no iframe owner to query.
- Console and network collector state belongs to each connection instance.
  Retiring a connection removes its managed callbacks and enabled flags;
  stable page connections keep their collectors. Re-enabling collection on a
  new iframe connection works even when it reuses the previous target ID.
- Command and parameter aliases match snake_case and CDP camelCase without
  guessing acronym capitalization. Parameter discovery reads exact JSON names
  from the official generated serializers, including `includeShadowDOM` and
  `allowUnsafeEvalBlockedByCSP`.
- Expected `nodriver.ProtocolException` failures use the SDK's `ToolError`,
  retaining Chrome's error code and reason. Unexpected failures and output
  validation errors retain the SDK's normal handling.

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

Verified locally on macOS on 2026-10-19 against official PyPI nodriver 0.50.6 and
Chrome 154, with temporary headless browser profiles and a loopback HTTP server.

- 105 regression/integration tests: **104 passed, 1 skipped** (MCP version 1.5.5).
- Native element input (including Unicode), CSS/XPath/best-match search,
  same-process frame selection and scoped operations passed.
- Trusted mouse/focus/single-click/double-click sequences and empty fills for
  input/textarea form models passed. Site-isolated iframe navigation to another
  site and back, document replacement/removal, stale UID rejection and selecting
  the main frame by ID passed.
- Coordinate single/double clicks produced the complete trusted mouse event
  sequence, correct click counts and released button state. Keyboard combinations
  reported correct modifier flags and physical codes, including literal plus
  keys; actual selection, deletion, character entry and Tab/Shift+Tab/Enter
  behavior passed. `type_text` also moved focus via `submit_key="Tab"`.
  Fault injection verified key cleanup after dispatch errors, release errors
  and cancellation, including preservation of the original failure.
- Real Chrome tests operated on two pages concurrently: form input, nested
  snapshots, distinct screenshot pixels, frame selection, navigation, viewport
  emulation, console/network collection and response bodies. A pending script
  did not block another page. Closing an earlier tab preserved stable IDs;
  closed IDs failed without falling back. A real stdio client discovered the
  new parameters, filled two pages concurrently and verified independent values.
- Action observation is installed before trusted input, then waits for bounded
  navigation/DOM settling before appended snapshots. Real Chrome verified delayed
  updates, click navigation, popup IDs, busy-DOM time limits and dialog
  interruption/cleanup. Script callers may skip DOM settling for read-only work.
  Cancellation, setup failure and stalled-observer cleanup passed fault injection.
- Checkbox/radio/ARIA switch state setting passed native and custom-control
  checks: idempotence, trusted clicks, indeterminate state, delayed ARIA updates,
  radio groups, disabled/unresponsive controls and invalid values. Same-process
  and site-isolated iframe controls passed. A real MCP client accepted boolean
  values and rejected numbers without coercing them into checked states.
- Native single/multiple file uploads, Unicode filenames and button-triggered
  file choosers passed in real Chrome. File chooser routing passed same-process
  and site-isolated iframe checks. Timeout and unsupported file counts left the
  next upload usable; cancellation removed managed callbacks and interception.
  A real stdio client uploaded multiple files through the new parameter.
- Background pages emulate focus with the official Emulation command, matching
  upstream multi-agent page initialization. Both pages retained `document.hasFocus()`
  across selection changes, navigation and emulation resets. Failed initialization
  remained retryable and connection retirement removed its preparation cache.
- Explicit console collection captured uncaught asynchronous exceptions and
  unhandled Promise rejections with source URL, line/column, execution context and
  native stack traces. Compact/verbose listing, detailed retrieval, navigation
  retention and disabling passed. Recreated iframe connections received fresh
  exception/revocation callbacks and produced new exception records; retired
  connections retained none. A real stdio client retrieved an uncaught error.
- Native CookieJar save/load/clear, isolated cookie contexts, batch expiry
  conversion and JSON session/localStorage round trips passed.
- Native image responses passed both direct calls and MCP content conversion;
  full-page output was checked against a 2400-pixel document.
- Network filtering/body retrieval, explicit console cleanup and nonempty trace
  stream export passed.
- Console and network collection resumed after a site-isolated iframe navigated
  back to the page's site and then returned to its original site with a recreated
  connection and the same target ID. Repeated enable calls retained one callback
  per event; console messages, fetch completion and response bodies were verified.
  Retiring an iframe cleared its collectors and preserved the page's collectors.
- A real stdio MCP client initialized, discovered tools and called command
  discovery successfully under both supported protocol modes. A fresh isolated
  `uv tool install` also initialized and discovered tools successfully.
  All 675 official command names resolved and command schemas generated.
- Ruff F checks, lock validation, whitespace checks and source/wheel builds passed.

See [the tool parity roadmap](tool-parity-roadmap.md) for the completed input
fixes and the prioritized capability/optimization backlog with acceptance criteria.

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
