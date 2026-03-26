# nodriver-mcp

An undetected alternative to [chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp). Powered by [nodriver](https://github.com/ultrafunkamsterdam/nodriver) to bypass anti-bot detection while providing the same MCP browser automation tools for AI coding agents.

## Why?

`chrome-devtools-mcp` uses Puppeteer under the hood, which exposes CDP/WebDriver fingerprints that are easily detected by anti-bot systems (Cloudflare, hCaptcha, etc.).

`nodriver` is the successor of `undetected-chromedriver`. It communicates directly via the CDP protocol without relying on a ChromeDriver binary or injecting Selenium/WebDriver markers, significantly reducing the chance of detection.

## Installation

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) (recommended):

```bash
# Install as an isolated tool (won't affect your global Python environment)
uv tool install "nodriver-mcp @ git+https://github.com/Saber-CC/nodriver-mcp.git@main"
```

> ⚠️ Avoid using `pip install` — this project depends on a [patched fork of nodriver](https://github.com/Saber-CC/nodriver/commit/ec323db) that fixes Chrome 146 compatibility (`sameParty` removed from Cookie, `privateNetworkRequestPolicy` renamed to `localNetworkAccessRequestPolicy`). Using `pip install` will overwrite the original nodriver in your global Python environment. `uv tool install` keeps it isolated.

## Upgrade

```bash
uv tool upgrade nodriver-mcp
```

## One-command MCP Client Setup

```bash
# Interactive client selector (terminal TUI)
nodriver-mcp install

# Install to specific clients
nodriver-mcp install claude,cursor,kiro

# Uninstall
nodriver-mcp uninstall claude

# List all supported clients
nodriver-mcp --list-clients

# Print MCP config JSON (for manual setup)
nodriver-mcp --config

# Project-level config (writes to .cursor/mcp.json, etc.)
nodriver-mcp install --scope project
```

Supported clients: Claude Desktop, Claude Code, Cursor, Windsurf, Codex, Gemini CLI, Copilot CLI, Kiro, VS Code, Cline, Roo Code, Amazon Q, Warp, Opencode, Trae.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `NODRIVER_HEADLESS` | Headless mode (`true`/`false`) | `false` |
| `NODRIVER_USER_DATA_DIR` | Chrome user data directory | Auto-created temp dir |
| `NODRIVER_BROWSER_PATH` | Chrome executable path | Auto-detected |
| `NODRIVER_PROXY` | Proxy server address | None |

## Tools (39)

Console and network collection are enabled automatically on each tab, so there is no separate `enable_*_collection` step.
For mobile-only sites, open `about:blank`, apply `emulate_device` or `emulate(...)`, then call `navigate_page` so the first request already carries mobile signals.

### Input Automation (10)
`click` · `click_at` · `hover` · `fill` · `fill_form` · `type_text` · `press_key` · `drag` · `upload_file` · `handle_dialog`

### Navigation (7)
`navigate_page` · `new_page` · `close_page` · `list_pages` · `select_page` · `wait_for` · `scroll_page`

### Screenshots & Debugging (5)
`take_screenshot` · `take_snapshot` · `evaluate_script` · `list_console_messages` · `get_console_message`

### Network Monitoring (2)
`list_network_requests` · `get_network_request`

### Device Emulation (3)
`emulate` · `emulate_device` · `resize_page`

### Performance (3)
`performance_start_trace` · `performance_stop_trace` · `take_memory_snapshot`

### Cookies & Storage (4)
`get_cookies` · `set_cookie` · `get_local_storage` · `set_local_storage`

### Session Management (3)
`save_session` · `load_session` · `list_sessions`

### Anti-Detection Helpers (2)
`cf_verify` · `bypass_insecure_warning`

## Comparison with chrome-devtools-mcp

| Feature | chrome-devtools-mcp | nodriver-mcp |
|---------|-----|-----|
| Browser backend | Puppeteer (ChromeDriver) | nodriver (direct CDP) |
| WebDriver fingerprint | ✗ Exposed | ✓ None |
| navigator.webdriver | ✗ true | ✓ undefined |
| Cloudflare bypass | ✗ | ✓ Built-in cf_verify |
| Install method | npx | uv tool install |
| Language | TypeScript / Node.js | Python |
| Core tool coverage | 29 tools | 38 tools |

Tools not implemented: `performance_analyze_insight` (requires DevTools frontend trace parser), `lighthouse_audit` (requires Lighthouse Node API), `screencast_start/stop` (requires ffmpeg + Puppeteer), extension management (experimental).

## License

[MIT](LICENSE)
