# nodriver-mcp

A Python MCP server for Chromium automation using official **nodriver 0.50.6**.
It exposes 75 MCP tools for browser startup, page interaction, native cookies and screenshots,
frames, network inspection, website WebMCP tools and the complete generated CDP API.

## Installation

Python 3.12+ and Chrome/Chromium are required. Use an isolated tool environment:

```bash
uv tool install "nodriver-mcp @ git+https://github.com/Saber-CC/nodriver-mcp.git@main"
uv tool upgrade nodriver-mcp
```

The official dependency is pinned to `nodriver==0.50.6`. A patched fork is no
longer required for Chrome 146's removed `sameParty` and renamed
`localNetworkAccessRequestPolicy` fields. Installing with pip inside a dedicated
virtual environment also provides isolation.

Version 1.5.22 uses the official MCP Python SDK 2.3.0 and its `MCPServer` API.
The dependency is pinned to `mcp==2.3.0` so fresh installs and tool upgrades use
the tested SDK version. Input validation and timeout failures retain their
actionable messages through the SDK's `ToolError` API.

For a source checkout:

```bash
uv sync
uv run nodriver-mcp
```

The Cloudflare image-template helper additionally requires the optional vision dependency:

```bash
uv sync --extra vision
```

## Configure MCP clients

```bash
nodriver-mcp install
nodriver-mcp install claude,cursor,kiro
nodriver-mcp install --scope project
nodriver-mcp uninstall claude
nodriver-mcp --list-clients
nodriver-mcp --config
```

The installer supports JSON and TOML client configurations. `--list-clients`
shows the available clients on the current platform.

## Browser configuration

The browser starts on the first browser tool call, using a **fresh temporary
profile by default**. Discovery of CDP bindings can run without launching Chrome.
The server uses stdio transport.

Ask your AI to open a website through a proxy or in a persistent session. It can
call `start_browser` directly, without editing your MCP client configuration:

```text
# "Open this website through http://127.0.0.1:7890"
start_browser(url="https://example.com", proxy="http://127.0.0.1:7890")

# "Open this website using a persistent session"
start_browser(url="https://example.com", persistent=true)

# Combine proxy and persistence; reuse the same profile name on future launches
start_browser(url="https://example.com", proxy="http://127.0.0.1:7890",
              persistent=true, profile="work")

# No startup settings requested: ordinary tools automatically use a temporary session
new_page(url="https://example.com")
```

Matching startup settings reuse the running browser. Changing the proxy,
persistence mode or profile automatically restarts Chrome and closes its current
pages. `restart=true` forces a restart even when the settings match; temporary
sessions then get a fresh profile. Other browser tools continue the chosen
session until it is closed. `close_browser()` closes Chrome, flushes persistent
storage, and removes temporary profiles, while keeping the MCP server available.
The next ordinary browser tool starts a new temporary session.

Persistent sessions reuse a Chrome profile across browser and MCP process
restarts. The `default` profile reuses `~/.nodriver-mcp/chrome-profile` (including
existing login data from earlier versions); named profiles use
`~/.nodriver-mcp/profiles/<name>`. Names contain 1-64 letters, digits, underscores
or hyphens and start with a letter or digit. A profile can be used by one browser
at a time. Unexpired persistent cookies and site storage are retained; session
cookies may end when Chrome exits, and websites can expire or revoke logins.
Isolated contexts remain temporary even inside a persistent browser.

This changes the earlier default of always using a persistent profile: request
`persistent=true` to continue using that profile. Setting `NODRIVER_USER_DATA_DIR`
chooses its directory but does not enable persistence by itself.

| Variable | Description | Default |
| --- | --- | --- |
| `NODRIVER_HEADLESS` | Run without a visible window | `false` |
| `NODRIVER_USER_DATA_DIR` | Directory for the explicit `persistent=true, profile="default"` session | `~/.nodriver-mcp/chrome-profile` |
| `NODRIVER_BROWSER_PATH` | Browser executable | Native auto-detection |
| `NODRIVER_PROXY` | Default browser proxy address, overridden by `start_browser(proxy=...)` | Chrome/system settings |
| `NODRIVER_BROWSER_ARGS` | JSON array of extra native browser arguments | `[]` |
| `NODRIVER_EXTENSIONS` | JSON array of extension directories/CRX paths, loaded with native Config.add_extension | `[]` |

`start_browser(proxy="")` forces direct access, overriding environment/system
proxy settings; omitting the argument inherits the configured defaults. Startup
proxies support HTTP, HTTPS, SOCKS4 and SOCKS5 addresses without credentials.
`create_browser_context` supports per-context proxies through the native
`Browser.create_context`, including nodriver's HTTP/SOCKS authentication forwarder.
Named contexts can also be reused with `new_page(isolated_context="name")`.
Cookies and storage tools operate in the selected context.

## Tools

### Browser startup and sessions

`start_browser`, `close_browser`.

Use `start_browser` before the first website request to choose a proxy and
persistence. `get_browser_info` includes the current launch settings and profile
directory. Persistent profiles are separate from the explicitly exported JSON
snapshots provided by `save_session` and `load_session`.

### Input and navigation

`click`, `click_at`, `hover`, `fill`, `fill_form`, `type_text`, `press_key`, `drag`,
`upload_file`, `handle_dialog`, `navigate_page`, `new_page`, `close_page`,
`list_pages`, `select_page`, `wait_for`, `scroll_page`.

`select_page` persists the selected page for subsequent tools. A background page
preserves that selection. `navigate_page` and `new_page` accept a device preset
before the first real navigation request.

`click` and `click_at` send trusted CDP mouse events, including both clicks for
a double click.
Clearing an input with `fill(uid, "")` dispatches `input` and `change` events so
the page's form state follows the displayed value.
`fill` and `fill_form` also set native checkbox/radio and ARIA checkbox/radio/switch
states. Use booleans or the strings `"true"`/`"false"`; setting the current state
again emits no extra click. Changes use trusted mouse events and verify the
result. Selected radios must be changed by selecting another option. Disabled,
read-only or unresponsive controls report a clear failure. Text/select values
continue to require strings.
Text filling uses native `Element.send_keys` for short values and a single
official `Input.insertText` command at 100 UTF-16 units or more. It supports
Unicode, controlled inputs, textarea and contenteditable controls, checks focus
and the retained value, and rejects disabled/read-only controls or maxlength
overflow before changing them. Batch filling refreshes frames once and waits
once; it stops on the first failed UID, navigation or dialog, reporting completed
fields and skipped entries. An unavailable appended snapshot preserves that
progress report.
`upload_file` accepts the existing `file_path` or a `file_paths` list. File inputs
delegate to native `Element.send_file`; an ordinary upload button can be clicked
to intercept and accept its file chooser. Files are validated locally, multiple
files require a multiple input/chooser, and chooser waiting has a configurable
`timeout` (3000 ms by default). Temporary interception and listeners are removed
after completion, failure or cancellation, including inside iframes.
`press_key` supplies modifier flags and physical key codes. Literal plus keys
work as `"+"`, `"Control++"` or `"Control+Shift++"`; Shift applies to letters and
punctuation. Invalid shortcuts fail before input is sent, and attempted keys
are released after errors or cancellation. On macOS, use `Meta+A` to select all;
native editing commands accompany the trusted keyboard events.

Interactive tools observe navigation, dialogs and popups before sending input.
They then wait for navigation and a quiet DOM within bounded observation windows,
so `include_snapshot` reflects the resulting page. Continuing DOM changes return
a time-limit notice; long-lived network requests do not hold up the tool.
`evaluate_script(wait_for_stable=false)` skips DOM settling for read-only scripts.
An unhandled dialog interrupts the action with its type/message and a reminder
to use `handle_dialog` on the same page. Temporary listeners and DOM observers
are removed after success, errors and cancellation.

`evaluate_script` handles dialogs with `dialog_action="accept"` by default.
Use `"dismiss"` to reject them, or `"manual"` to return immediately with the
dialog details and finish via `handle_dialog`. `prompt_text` supplies accepted
prompt content. Script results report `completed`, `interrupted`, `failed` or
`timed_out`; the JSON value remains in its code block. `timeout` bounds script
execution (30000 ms by default, 0 to disable). Open dialogs are tracked per page,
so blocked interactions fail promptly while diagnostic tools remain usable.

Script `format` can be `auto` (default), `function` or `script`. Auto evaluates
expressions and statements once, invoking a returned function when appropriate.
For calls without element arguments, the native deadline also bounds synchronous loops. Temporary element
handles are released after execution. `file_path` saves the complete JSON result;
`max_output_chars` limits inline output (default 20000). Larger results are saved
automatically under `~/.nodriver-mcp/artifacts/scripts`, with a clearly labeled
preview and the full file path. Set `NODRIVER_ARTIFACTS_DIR` to change this root.

Use `evaluate_script(source_path="/path/to/script.js", format="script")` to read
UTF-8 source from the MCP server's local filesystem. Relative paths and `file:`
URLs also work. Provide exactly one of `function` or `source_path`; `file_path`
remains the output path. Source errors are checked before selecting a page or
launching Chrome. File sources support the same formats, element arguments,
dialog policy, timeout and output controls as inline scripts.

### Native frames and element search

Page tools accept optional `page_id` for concurrent work on multiple tabs.
Use the stable string ID shown by `list_pages`, returned in `start_browser`,
or printed by `new_page`:

```json
{"function": "document.title", "page_id": "CHROME_TARGET_ID"}
```

This `evaluate_script` call targets that page without selecting or bringing it
to the front. Page focus is emulated, so background tabs retain focus semantics
for applications that check `document.hasFocus()`. The same parameter is available
on snapshots, input, navigation,
screenshots, frames, emulation, cookies, console/network tools and page CDP calls.
Nested work such as `fill_form` and `include_snapshot` stays on the same page,
even if another call changes `select_page`. A closed/unknown ID fails explicitly.
Omitting `page_id` keeps the selected-page workflow. Numeric list indexes remain
accepted for compatibility; prefer string IDs because indexes move as tabs close.

Frame selection and collected records belong to each page. Cookies remain shared
by pages in the same browser context, as in Chrome; use `isolated_context` for
separate cookie jars. Window resizing affects the shared Chrome window; use
viewport emulation for independent page dimensions. Screen recording is tracked
per page. Chrome permits one browser-wide performance trace at a time; an
explicit `page_id` on trace stop must match its originating page. Browser/session
startup and shutdown affect all pages. Concurrent actions on the same page can
still interfere with each other's focus or navigation.

`list_frames`, `select_frame`, `find_elements`.

`list_frames` combines native `Tab.get_frames()` with the page frame tree.
`select_frame(frame_id="")` or the main frame's ID returns to the main page.
Snapshots and scripts respect the selected frame;
`take_snapshot(include_frames=true)` includes child frames. Element UIDs retain
their page/frame connection, so an old UID cannot silently operate on another page.
Frame connections refresh when navigation changes their process ownership;
elements from a replaced or removed frame document require new UIDs.

`find_elements` uses native text, CSS or XPath search and returns actionable UIDs:

```text
find_elements(selector="input[type=email]")
find_elements(text="Continue")
find_elements(text="Continue", best_match=true)
find_elements(xpath="//button[contains(., 'Continue')]")
```

Media emulation accepts `forced_colors`, `prefers_contrast` and
`prefers_reduced_motion` alongside `color_scheme`. Updates merge per page;
`auto` resets only the named feature. `extra_http_headers` accepts a string-valued
object or JSON string, replaces extra headers and persists across navigation.
Use `{}` or `""` to clear headers. `reset_emulation` clears all media and headers.
New cross-process iframes receive these overrides before their initial scripts and
subresources run, including nested frames. Later changes and resets also update
existing frame sessions; no `list_frames` call is needed to initialize them.

### Screenshots and debugging

`take_screenshot`, `take_snapshot`, `evaluate_script`, `enable_console_collection`,
`disable_console_collection`, `list_console_messages`, `get_console_message`,
`get_browser_info`, `get_connection_history`.

Use text snapshots for reading pages. Ordinary PNG/JPEG screenshots delegate to
`Tab.save_screenshot(as_base64=True)` and return native MCP image content, or
save a file when `file_path` is supplied. Element crops, WebP and explicit
quality use the official generated CDP screenshot command because those
arguments are not exposed by the native helper.

`take_screenshot(max_width=1280, max_height=720)` limits physical output pixels
proportionally for viewport, full-page and element captures, including high DPI.
Limits use native CDP scaling and never upscale. Both inline images and saved
files honor the limits; omitting them retains the original resolution.

Console collection is opt-in and does not enable `Runtime` during ordinary
browsing. Explicit console/event tools can enable it when needed.
Once enabled, it captures `console.*`, uncaught JavaScript errors and unhandled
Promise rejections. `list_console_messages(include_stack_traces=true)` includes
native stacks; `get_console_message` shows stack and source/context metadata by
default. Disabling or retiring a connection removes all managed console/error
callbacks. Use `enable_console_collection(source_maps=true)` to opt into Debugger
metadata and async stacks. Console details and requested list stacks then resolve
source-map v3 original functions/locations/source lines while retaining generated
positions. On initial startup with source maps enabled, script metadata is captured
before Runtime replays buffered logs, so those logs can also map to original sources.
External maps load through Chrome with its proxy and cookies; inline
and indexed maps are supported. Failed or slow maps retain raw positions and a
diagnostic. `resolve_source_maps=false` skips resolution; `source_maps=false`
disables owned Debugger collection. Ordinary browsing keeps these domains disabled.
Script identities and captured log metadata remain separate across renderer changes,
even when Chrome reuses a script ID. Cached old maps remain usable; uncached external
maps from a retired execution context retain generated positions and a diagnostic.

`list_pages(include_service_workers=true)` also lists running service workers
and their stable `service_worker_id`, including extension IDs when applicable.
Pass that ID to `evaluate_script` or console enable/disable/list/get tools.
Worker calls exclude `page_id` and element arguments, leave the selected page
unchanged and skip DOM/dialog handling. Script formats, source/output files and
timeouts work as on pages. Logs and exceptions remain isolated by worker.
Stopped workers disappear; rediscover after a restart rather than reusing an old ID.
Loading unpacked extensions via CDP requires Chrome's
`--enable-unsafe-extension-debugging` startup argument.

### Cookies, storage and sessions

`get_cookies`, `set_cookie`, `set_cookies`, `clear_cookies`, `save_cookies`,
`load_cookies`, `get_local_storage`, `set_local_storage`, `save_session`,
`load_session`, `list_sessions`.

Cookie tools directly use the official `CookieJar` methods. `set_cookies`
accepts a list of CDP cookie objects, including `expires`, `sameSite`,
`httpOnly` and `partitionKey`; snake_case top-level fields also work.

`save_cookies` and `load_cookies` use nodriver's native pickle format for locally
created cookie files. `save_session` retains the JSON format and also stores
localStorage and page URLs. Legacy JSON sessions can still be loaded, with
cookie expiry converted into the official typed value before a native batch set.

### Network, emulation and performance

`list_network_requests`, `get_network_request`, `configure_network_capture`,
`emulate`, `emulate_device`, `reset_emulation`, `resize_page`,
`performance_start_trace`, `performance_stop_trace`, `performance_analyze_trace`,
`performance_analyze_insight`, `take_memory_snapshot`.

Network collection starts before a new page's first request. Request IDs remain
stable under filtering and pagination; body retrieval uses the connection that
originated the request. `configure_network_capture` configures the new native
durable-message API for response bodies across process changes.
Request details retain independent redirect hops, preceding-chain IDs and extra
request/response headers, including wire status. `get_network_request` reports
body availability and accepts `include_body=false` and `max_body_chars` (5000 by
default). Previews are labeled when truncated; file exports retain complete data.
Intermediate redirects and retired connections never query a later request's body.
Current network records are limited to 1000 per page.

Console and network collectors follow the connection instance. Re-enabling them
after an iframe connection is recreated initializes the new connection, even
when Chrome reuses its target ID.

Trace export consumes the native `TracingComplete.stream` using `IO.read` and
`IO.close`. Every stopped trace is saved in full, by default under
`~/.nodriver-mcp/artifacts/traces`; a path passed at start is retained for manual
stop. JSON and `.gz` outputs are supported. The response reports the absolute
path, event count, duration, categories/config and Chrome's data-loss flag.
`duration_ms` controls automatic recording (default 5000), `categories` can
replace DevTools defaults and `ignore_cache` applies during reload. Stopping
uses the original connection after page switches. Cancellation and failure
release recording state; closing its page/browser first finishes the trace.

Set `analyze=true` on trace start/stop to append real DevTools Insights and
observed Web Vitals. Saved JSON/gzip files can also be analyzed offline with
`performance_analyze_trace(trace_path=...)`; omit the path to use the latest
recording. `performance_analyze_insight` expands an insight set/name into native
causes, request details and estimated savings. Complete JSON reports are saved
under `artifacts/performance` or the specified `file_path`; inline previews are
bounded by `max_output_chars`. Analysis failures preserve the raw recording.

Analysis requires Node.js 22.12+ (`NODRIVER_NODE_PATH` can select its executable).
On first analysis, the server fetches the published `chrome-devtools-mcp@1.11.0`
engine into `~/.cache/nodriver-mcp/trace-engine`, verifies its pinned SHA-512 and
reuses it on later calls. `NODRIVER_TRACE_ENGINE_CACHE_DIR` changes that directory;
`NODRIVER_DEVTOOLS_PACKAGE` selects an existing published package for offline use.
Browser automation and raw trace capture use the Python dependencies alone.

### Website WebMCP tools

`list_webmcp_tools`, `call_webmcp_tool`, `get_webmcp_invocation`,
`cancel_webmcp_invocation`.

These discover business tools explicitly registered by a website. They extend
browser automation; sites without registered WebMCP tools continue to use
snapshots, clicks and scripts.

```text
list_webmcp_tools()
call_webmcp_tool(tool_name="search_products", input={"query": "keyboard"})
```

Pass `wait_for_result=false` to receive an invocation ID, then read its result
or cancel it. Supply `frame_id` when the same tool name exists in multiple frames.
The browser must expose the website's WebMCP registration API. A successful CDP
`WebMCP.enable` call alone does not establish that this page API is available.
Use `NODRIVER_BROWSER_ARGS` for feature flags required by a particular browser build.

### Additional 0.50.6 browser capabilities

`start_screen_recording`, `stop_screen_recording`,
`get_annotated_page_content`, `list_extensions`, `trigger_extension_action`,
`get_ad_info`, `get_crash_report_context`, `create_browser_context`,
`close_browser_context`.

Native screen recording writes Chrome's stream without a separate ffmpeg
process. Annotated page content is a base64 protobuf, distinct from readable
accessibility snapshots. These experimental capabilities depend on the browser
build and enabled features; protocol failures are reported rather than simulated.
`trigger_extension_action` brings its requested page to the front before invoking
the browser toolbar action. An explicit `page_id` keeps the MCP's default page
selection unchanged.

### Complete official CDP coverage

`list_cdp_commands`, `send_cdp_command`, `list_cdp_events`, `subscribe_cdp_events`,
`get_cdp_events`, `unsubscribe_cdp_events`.

All 675 generated official commands are discoverable, including every one of the
58 commands added since the old 0.48.1 baseline. This covers specialized domains
such as SmartCardEmulation, DigitalCredentials, new display/keyboard/CPU emulation,
DOM/CSS changes, virtual cameras and device-bound sessions.

```text
list_cdp_commands(domain="Emulation", query="keyboard")
send_cdp_command(domain="Emulation", command="setVirtualKeyboardGeometryOverride",
                 parameters={"x": 0, "y": 600, "width": 412, "height": 315})
subscribe_cdp_events(events=["SmartCardEmulation.establishContextRequested"])
get_cdp_events(subscription_id="1")
unsubscribe_cdp_events(subscription_id="1")
```

Use `scope="browser"` for browser-wide commands. Top-level command parameters
accept Python snake_case or CDP camelCase, including `HTML`, `HTTP` and `CSP`
acronyms; nested objects use CDP JSON names.
Values are constructed with nodriver's generated enums, IDs and dataclasses.
Chrome protocol failures retain their error code and reason in MCP tool errors.
Discovering a binding does not imply that every Chrome version implements it.

Official nodriver 0.50.6 has a known parsing limitation:
`ServiceWorker.workerVersionUpdated` can lose recursive `or`/`not` conditions
inside `typedRouterRules`. Event discovery and the relevant subscription,
retrieval and connection-history tool descriptions identify this limitation.
An empty condition `{}` does not establish an unconditional match; verify actual
request/cache behavior before drawing routing conclusions. Chrome still executes
the original rules. The MCP continues to use the official package; these hints
do not restore the missing fields.

## Validation and upgrade notes

```bash
uv run python -m unittest discover -s tests -v
NODRIVER_MCP_LIVE_TESTS=1 uv run python -m unittest discover -s tests -v
NODRIVER_MCP_INSTALL_TESTS=1 uv run python -m unittest discover -s tests -p test_installation.py -v
```

Live tests launch headless Chrome with temporary profiles and a local HTTP
server. The opt-in installation test performs a fresh `uv tool install` in
temporary tool directories, resolving dependencies independently of `uv.lock`,
and verifies the installed server's MCP handshake and tool discovery.
The stdio tests cover the legacy handshake and modern protocol, tool errors
without disconnecting the client, PNG image responses, and persistent
cookies/storage across MCP process restarts.

## License

[MIT](LICENSE)
