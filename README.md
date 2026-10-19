# nodriver-mcp

A Python MCP server for Chromium automation using official **nodriver 0.50.6**.
It exposes 74 MCP tools for browser startup, page interaction, native cookies and screenshots,
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

The MCP SDK is constrained to `mcp>=1.26.0,<2` because the server uses its 1.x
FastMCP API. Version 1.3.1 fixes fresh installs and tool upgrades that previously
resolved MCP SDK 2.x and exited during startup with `Connection closed`.

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

### Native frames and element search

`list_frames`, `select_frame`, `find_elements`.

`list_frames` combines native `Tab.get_frames()` with the page frame tree.
`select_frame(frame_id="")` returns to the main page. Snapshots and scripts
respect the selected frame; `take_snapshot(include_frames=true)` includes child
frames. Element UIDs retain their page/frame connection, so an old UID cannot
silently operate on another page.

`find_elements` uses native text, CSS or XPath search and returns actionable UIDs:

```text
find_elements(selector="input[type=email]")
find_elements(text="Continue")
find_elements(text="Continue", best_match=true)
find_elements(xpath="//button[contains(., 'Continue')]")
```

### Screenshots and debugging

`take_screenshot`, `take_snapshot`, `evaluate_script`, `enable_console_collection`,
`disable_console_collection`, `list_console_messages`, `get_console_message`,
`get_browser_info`, `get_connection_history`.

Use text snapshots for reading pages. Ordinary PNG/JPEG screenshots delegate to
`Tab.save_screenshot(as_base64=True)` and return native MCP image content, or
save a file when `file_path` is supplied. Element crops, WebP and explicit
quality use the official generated CDP screenshot command because those
arguments are not exposed by the native helper.

Console collection is opt-in and does not enable `Runtime` during ordinary
browsing. Explicit console/event tools can enable it when needed.

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
`performance_start_trace`, `performance_stop_trace`, `take_memory_snapshot`.

Network collection starts before a new page's first request. Request IDs remain
stable under filtering and pagination; body retrieval uses the connection that
originated the request. `configure_network_capture` configures the new native
durable-message API for response bodies across process changes.

Trace export consumes the native `TracingComplete.stream` using `IO.read` and
`IO.close`. Stopping a trace uses its original connection even after selecting
another page.

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

`find_in_page`, `start_screen_recording`, `stop_screen_recording`,
`get_annotated_page_content`, `list_extensions`, `trigger_extension_action`,
`get_ad_info`, `get_crash_report_context`, `create_browser_context`,
`close_browser_context`.

Native screen recording writes Chrome's stream without a separate ffmpeg
process. Annotated page content is a base64 protobuf, distinct from readable
accessibility snapshots. These experimental capabilities depend on the browser
build and enabled features; protocol failures are reported rather than simulated.

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
accept Python snake_case or CDP camelCase; nested objects use CDP JSON names.
Values are constructed with nodriver's generated enums, IDs and dataclasses.
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
See [the upgrade coverage and implementation notes](docs/nodriver-0.50.6.md)
for the complete added-command inventory and native API decisions.

## License

[MIT](LICENSE)
