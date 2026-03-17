"""
nodriver-mcp: An undetected Chrome automation MCP server.

Uses nodriver (successor of undetected-chromedriver) as the browser backend,
providing the same MCP tool interface as chrome-devtools-mcp but without
exposing CDP/WebDriver fingerprints that get detected by anti-bot systems.
"""

import asyncio
import base64
import json
import logging
import os
import sys
import time
from io import BytesIO
from typing import Any

import nodriver as uc
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("nodriver-mcp")


# ---------------------------------------------------------------------------
# Global browser state
# ---------------------------------------------------------------------------
_browser: uc.Browser | None = None
_browser_lock = asyncio.Lock()


async def _get_browser() -> uc.Browser:
    """Start the browser on first tool call (lazy init, protected by mutex)."""
    global _browser
    async with _browser_lock:
        if _browser is None or _browser.stopped:
            headless = os.environ.get("NODRIVER_HEADLESS", "").lower() in ("1", "true", "yes")
            user_data_dir = os.environ.get("NODRIVER_USER_DATA_DIR", None)
            browser_path = os.environ.get("NODRIVER_BROWSER_PATH", None)
            proxy = os.environ.get("NODRIVER_PROXY", None)

            kwargs: dict[str, Any] = {"headless": headless}
            if user_data_dir:
                kwargs["user_data_dir"] = user_data_dir
            if browser_path:
                kwargs["browser_executable_path"] = browser_path

            _browser = await uc.start(**kwargs)

            if proxy:
                logger.info("Proxy configured: %s", proxy)

            logger.info("Browser started (headless=%s)", headless)
    return _browser


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "nodriver-mcp",
    instructions=(
        "Undetected Chrome browser automation via nodriver. "
        "Drop-in replacement for chrome-devtools-mcp that avoids CDP fingerprint detection. "
        "IMPORTANT: Always use take_snapshot instead of take_screenshot to read page content. "
        "take_snapshot returns searchable HTML text and is much faster and smaller. "
        "Only use take_screenshot when you specifically need a visual image for layout checks or visual regression. "
        "NOTE: The browser is launched lazily on the first tool call. "
        "The first invocation may take a few extra seconds for Chrome to start — this is normal, just wait for it."
    ),
)


async def _active_tab() -> uc.Tab:
    """Return the currently active tab, or the main tab."""
    browser = await _get_browser()
    if browser.tabs:
        return browser.tabs[-1]
    return browser.main_tab


# ---------------------------------------------------------------------------
# Shared state for console / network collection
# ---------------------------------------------------------------------------
_console_messages: list[dict] = []
_network_requests: list[dict] = []
_tracing_active = False

# ---------------------------------------------------------------------------
# Tools (alphabetical order, matching chrome-devtools-mcp convention)
# ---------------------------------------------------------------------------

@mcp.tool()
async def bypass_insecure_warning() -> str:
    """Click through the browser's insecure connection warning page."""
    tab = await _active_tab()
    await tab.bypass_insecure_connection_warning()
    return json.dumps({"status": "ok"})


@mcp.tool()
async def cf_verify() -> str:
    """Attempt to solve a Cloudflare verification challenge.

    Uses nodriver's built-in CF verification bypass.
    Requires opencv-python to be installed.
    """
    tab = await _active_tab()
    try:
        await tab.verify_cf()
        return json.dumps({"status": "ok", "message": "CF verification attempted"})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def click(selector: str = "", text: str = "", timeout: int = 10) -> str:
    """Click an element found by CSS selector or text.

    Args:
        selector: CSS selector of the element to click.
        text: Visible text of the element to click.
        timeout: Max seconds to wait for the element.
    """
    tab = await _active_tab()
    try:
        if selector:
            elem = await tab.select(selector, timeout=timeout)
        elif text:
            elem = await tab.find(text, best_match=True, timeout=timeout)
        else:
            return json.dumps({"error": "Provide either selector or text"})
        await elem.mouse_click()
        await tab
        return json.dumps({"status": "clicked", "tag": elem.tag_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def click_at(x: int, y: int, dbl_click: bool = False) -> str:
    """Click at specific coordinates on the page.

    Args:
        x: The x coordinate.
        y: The y coordinate.
        dbl_click: Set to true for double clicks.
    """
    tab = await _active_tab()
    if dbl_click:
        await tab.mouse_click(x, y)
        await tab.mouse_click(x, y)
    else:
        await tab.mouse_click(x, y)
    return json.dumps({"status": "clicked", "x": x, "y": y})


@mcp.tool()
async def close_page() -> str:
    """Close the current active tab."""
    tab = await _active_tab()
    await tab.close()
    return json.dumps({"status": "ok", "message": "Tab closed"})


@mcp.tool()
async def drag(source_selector: str, target_selector: str, timeout: int = 10) -> str:
    """Drag from one element to another.

    Args:
        source_selector: CSS selector of the drag source.
        target_selector: CSS selector of the drop target.
        timeout: Max seconds to wait.
    """
    tab = await _active_tab()
    try:
        src = await tab.select(source_selector, timeout=timeout)
        dst = await tab.select(target_selector, timeout=timeout)
        src_rect = await src.get_position()
        dst_rect = await dst.get_position()
        if src_rect and dst_rect:
            await tab.mouse_drag(
                (src_rect.x + src_rect.width / 2, src_rect.y + src_rect.height / 2),
                (dst_rect.x + dst_rect.width / 2, dst_rect.y + dst_rect.height / 2),
            )
            return json.dumps({"status": "dragged"})
        return json.dumps({"error": "Could not determine element positions"})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def emulate(
    network_conditions: str = "",
    cpu_throttling_rate: float = 0,
    geolocation: str = "",
    user_agent: str = "",
    color_scheme: str = "",
    viewport: str = "",
) -> str:
    """Emulate various device/network conditions on the selected page.

    Args:
        network_conditions: Network throttle preset, e.g. "Slow 3G", "Fast 3G", "Offline". Empty to disable.
        cpu_throttling_rate: CPU slowdown factor (1-20). 0 or 1 to disable.
        geolocation: Geolocation as "latitude,longitude", e.g. "37.7749,-122.4194". Empty to clear.
        user_agent: User agent string. Empty to clear.
        color_scheme: "dark", "light", or "auto". Empty to skip.
        viewport: Viewport as "widthxheight", e.g. "375x812". Empty to skip.
    """
    tab = await _active_tab()
    results = []

    if network_conditions:
        import nodriver.cdp.network as cdp_net
        presets = {
            "offline": {"offline": True, "latency": 0, "download": 0, "upload": 0},
            "slow 3g": {"offline": False, "latency": 2000, "download": 50000, "upload": 50000},
            "fast 3g": {"offline": False, "latency": 563, "download": 180000, "upload": 84375},
        }
        p = presets.get(network_conditions.lower(), presets.get("fast 3g"))
        await tab.send(cdp_net.emulate_network_conditions(
            offline=p["offline"],
            latency=p["latency"],
            download_throughput=p["download"],
            upload_throughput=p["upload"],
        ))
        results.append(f"network={network_conditions}")

    if cpu_throttling_rate and cpu_throttling_rate > 1:
        import nodriver.cdp.emulation as cdp_emu
        await tab.send(cdp_emu.set_cpu_throttling_rate(rate=cpu_throttling_rate))
        results.append(f"cpu_throttle={cpu_throttling_rate}x")

    if geolocation:
        import nodriver.cdp.emulation as cdp_emu
        parts = geolocation.split(",")
        lat, lng = float(parts[0]), float(parts[1])
        await tab.send(cdp_emu.set_geolocation_override(latitude=lat, longitude=lng, accuracy=1.0))
        results.append(f"geolocation={lat},{lng}")

    if user_agent:
        import nodriver.cdp.network as cdp_net
        await tab.send(cdp_net.set_user_agent_override(user_agent=user_agent))
        results.append("user_agent set")

    if color_scheme and color_scheme != "auto":
        import nodriver.cdp.emulation as cdp_emu
        await tab.send(cdp_emu.set_emulated_media(
            features=[cdp_emu.MediaFeature(name="prefers-color-scheme", value=color_scheme)]
        ))
        results.append(f"color_scheme={color_scheme}")

    if viewport:
        import nodriver.cdp.emulation as cdp_emu
        parts = viewport.lower().split("x")
        w, h = int(parts[0]), int(parts[1])
        mobile = len(parts) > 2 and "mobile" in parts[2:]
        await tab.send(cdp_emu.set_device_metrics_override(
            width=w, height=h, device_scale_factor=1.0, mobile=mobile,
        ))
        results.append(f"viewport={viewport}")

    return json.dumps({"status": "ok", "applied": results})


@mcp.tool()
async def emulate_device(
    width: int = 375,
    height: int = 812,
    device_scale_factor: float = 3.0,
    mobile: bool = True,
    user_agent: str = "",
) -> str:
    """Emulate a mobile device or custom viewport.

    Args:
        width: Viewport width in pixels.
        height: Viewport height in pixels.
        device_scale_factor: Device scale factor (e.g. 2.0 for retina).
        mobile: Whether to emulate a mobile device.
        user_agent: Custom user agent string (optional).
    """
    tab = await _active_tab()
    import nodriver.cdp.emulation as cdp_emu

    await tab.send(cdp_emu.set_device_metrics_override(
        width=width,
        height=height,
        device_scale_factor=device_scale_factor,
        mobile=mobile,
    ))
    if user_agent:
        import nodriver.cdp.network as cdp_net
        await tab.send(cdp_net.set_user_agent_override(user_agent=user_agent))

    return json.dumps({"status": "ok", "viewport": f"{width}x{height}", "mobile": mobile})


@mcp.tool()
async def enable_console_collection() -> str:
    """Start collecting console messages from the current page."""
    tab = await _active_tab()
    import nodriver.cdp.runtime as cdp_runtime

    async def _on_console(event: cdp_runtime.ConsoleAPICalled):
        msg = {
            "type": event.type_.value,
            "text": " ".join(str(a.value or a.description or "") for a in event.args),
            "timestamp": str(event.timestamp),
        }
        _console_messages.append(msg)
        if len(_console_messages) > 200:
            _console_messages.pop(0)

    await tab.send(cdp_runtime.enable())
    tab.add_handler(cdp_runtime.ConsoleAPICalled, _on_console)
    return json.dumps({"status": "ok", "message": "Console collection enabled"})


@mcp.tool()
async def enable_network_collection() -> str:
    """Start collecting network requests from the current page."""
    tab = await _active_tab()
    import nodriver.cdp.network as cdp_net

    async def _on_request(event: cdp_net.RequestWillBeSent):
        _network_requests.append({
            "id": str(event.request_id),
            "url": event.request.url,
            "method": event.request.method,
            "timestamp": str(event.timestamp),
            "type": str(event.type_.value) if event.type_ else "unknown",
        })
        if len(_network_requests) > 500:
            _network_requests.pop(0)

    await tab.send(cdp_net.enable())
    tab.add_handler(cdp_net.RequestWillBeSent, _on_request)
    return json.dumps({"status": "ok", "message": "Network collection enabled"})


@mcp.tool()
async def evaluate_script(expression: str, await_promise: bool = False) -> str:
    """Execute JavaScript in the current page and return the result.

    Args:
        expression: JavaScript expression to evaluate.
        await_promise: If True, await the result if it's a Promise.
    """
    tab = await _active_tab()
    try:
        result = await tab.evaluate(expression, await_promise=await_promise)
        return json.dumps({"status": "ok", "result": str(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def fill(selector: str, value: str, timeout: int = 10) -> str:
    """Fill an input field with the given value (clears existing content first).

    Args:
        selector: CSS selector of the input element.
        value: The text value to fill in.
        timeout: Max seconds to wait for the element.
    """
    tab = await _active_tab()
    try:
        elem = await tab.select(selector, timeout=timeout)
        await elem.clear_input()
        await elem.send_keys(value)
        await tab
        return json.dumps({"status": "filled", "selector": selector})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def fill_form(fields: dict[str, str]) -> str:
    """Fill multiple form fields at once.

    Args:
        fields: A dict mapping CSS selectors to values, e.g. {"#email": "test@example.com", "#password": "secret"}.
    """
    tab = await _active_tab()
    results = []
    for sel, val in fields.items():
        try:
            elem = await tab.select(sel, timeout=5)
            await elem.clear_input()
            await elem.send_keys(val)
            results.append({"selector": sel, "status": "ok"})
        except Exception as e:
            results.append({"selector": sel, "status": "error", "message": str(e)})
    await tab
    return json.dumps(results)


@mcp.tool()
async def get_console_messages() -> str:
    """Get recent console messages from the page.

    Note: Console message collection must be enabled first by calling enable_console_collection.
    """
    return json.dumps({
        "status": "ok",
        "messages": list(_console_messages[-50:]),
    })


@mcp.tool()
async def get_cookies(url: str = "") -> str:
    """Get all cookies, optionally filtered by URL.

    Args:
        url: If provided, only return cookies for this URL.
    """
    tab = await _active_tab()
    import nodriver.cdp.network as cdp_net
    if url:
        cookies = await tab.send(cdp_net.get_cookies(urls=[url]))
    else:
        cookies = await tab.send(cdp_net.get_cookies())
    result = []
    for c in cookies:
        result.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
            "secure": c.secure,
            "http_only": c.http_only,
        })
    return json.dumps({"count": len(result), "cookies": result})


@mcp.tool()
async def get_local_storage() -> str:
    """Get all localStorage items from the current page."""
    tab = await _active_tab()
    data = await tab.get_local_storage()
    return json.dumps({"status": "ok", "data": data})


@mcp.tool()
async def get_network_request(request_id: str) -> str:
    """Get details of a specific network request by its ID.

    Args:
        request_id: The request ID from list_network_requests.
    """
    tab = await _active_tab()
    import nodriver.cdp.network as cdp_net
    try:
        body = await tab.send(cdp_net.get_response_body(cdp_net.RequestId(request_id)))
        return json.dumps({"status": "ok", "body": body[0][:50000], "base64_encoded": body[1]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def handle_dialog(action: str = "accept", prompt_text: str = "") -> str:
    """Handle a browser dialog (alert, confirm, prompt).

    Args:
        action: "accept" or "dismiss".
        prompt_text: Optional text to enter into a prompt dialog.
    """
    tab = await _active_tab()
    import nodriver.cdp.page as cdp_page
    if action == "accept":
        await tab.send(cdp_page.handle_java_script_dialog(accept=True, prompt_text=prompt_text))
    else:
        await tab.send(cdp_page.handle_java_script_dialog(accept=False))
    return json.dumps({"status": "ok", "action": action})


@mcp.tool()
async def hover(selector: str = "", text: str = "", timeout: int = 10) -> str:
    """Hover over an element.

    Args:
        selector: CSS selector of the element.
        text: Visible text of the element.
        timeout: Max seconds to wait.
    """
    tab = await _active_tab()
    try:
        if selector:
            elem = await tab.select(selector, timeout=timeout)
        elif text:
            elem = await tab.find(text, best_match=True, timeout=timeout)
        else:
            return json.dumps({"error": "Provide either selector or text"})
        await elem.mouse_move()
        return json.dumps({"status": "hovered", "tag": elem.tag_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def list_network_requests(url_filter: str = "") -> str:
    """List collected network requests, optionally filtered by URL substring.

    Args:
        url_filter: Only return requests whose URL contains this string.
    """
    filtered = _network_requests
    if url_filter:
        filtered = [r for r in _network_requests if url_filter in r["url"]]
    return json.dumps({"count": len(filtered), "requests": filtered[-100:]})


@mcp.tool()
async def list_pages() -> str:
    """List all open tabs/pages with their URLs and titles."""
    browser = await _get_browser()
    pages = []
    for i, tab in enumerate(browser.tabs):
        pages.append({
            "index": i,
            "url": tab.target.url,
            "title": tab.target.title,
        })
    return json.dumps(pages)


@mcp.tool()
async def navigate(url: str, new_tab: bool = False) -> str:
    """Navigate to a URL. Optionally open in a new tab.

    Args:
        url: The URL to navigate to.
        new_tab: If True, open the URL in a new tab.
    """
    browser = await _get_browser()
    tab = await browser.get(url, new_tab=new_tab)
    await tab
    return json.dumps({"status": "ok", "url": tab.target.url, "title": tab.target.title})


@mcp.tool()
async def navigate_page(
    type: str = "url",
    url: str = "",
    ignore_cache: bool = False,
) -> str:
    """Navigate the page by URL, back/forward in history, or reload.

    Args:
        type: One of "url", "back", "forward", "reload".
        url: Target URL (only for type=url).
        ignore_cache: Whether to ignore cache on reload.
    """
    tab = await _active_tab()
    if type == "url":
        if not url:
            return json.dumps({"error": "URL is required for type=url"})
        await tab.get(url)
        await tab
    elif type == "back":
        await tab.back()
        await tab
    elif type == "forward":
        await tab.forward()
        await tab
    elif type == "reload":
        await tab.reload(ignore_cache=ignore_cache)
        await tab
    else:
        return json.dumps({"error": f"Unknown type: {type}"})
    return json.dumps({"status": "ok", "url": tab.target.url})


@mcp.tool()
async def new_page(url: str = "about:blank") -> str:
    """Open a new tab/page and navigate to the given URL.

    Args:
        url: URL to open in the new tab.
    """
    browser = await _get_browser()
    tab = await browser.get(url, new_tab=True)
    await tab
    return json.dumps({"status": "ok", "url": tab.target.url, "title": tab.target.title})


@mcp.tool()
async def performance_start_trace(reload: bool = True) -> str:
    """Start a performance trace on the selected page.

    Args:
        reload: Whether to reload the page after starting the trace.
    """
    global _tracing_active
    tab = await _active_tab()
    import nodriver.cdp.tracing as cdp_tracing

    if _tracing_active:
        return json.dumps({"error": "A trace is already running. Stop it first."})

    categories = [
        "-*", "blink.console", "blink.user_timing", "devtools.timeline",
        "disabled-by-default-devtools.screenshot",
        "disabled-by-default-devtools.timeline",
        "disabled-by-default-devtools.timeline.frame",
        "disabled-by-default-devtools.timeline.stack",
        "disabled-by-default-v8.cpu_profiler",
        "latencyInfo", "loading", "v8.execute", "v8",
    ]
    await tab.send(cdp_tracing.start(categories=",".join(categories), transfer_mode="ReturnAsStream"))
    _tracing_active = True

    if reload:
        await tab.reload()
        await tab.sleep(5)

    return json.dumps({"status": "tracing", "message": "Trace started. Use performance_stop_trace to stop."})


@mcp.tool()
async def performance_stop_trace(file_path: str = "") -> str:
    """Stop the active performance trace and return summary.

    Args:
        file_path: Optional path to save the raw trace JSON.
    """
    global _tracing_active
    tab = await _active_tab()
    import nodriver.cdp.tracing as cdp_tracing

    if not _tracing_active:
        return json.dumps({"error": "No trace is running."})

    trace_chunks = []

    async def on_data(event: cdp_tracing.DataCollected):
        trace_chunks.extend(event.value)

    tab.add_handler(cdp_tracing.DataCollected, on_data)

    done_event = asyncio.Event()

    async def on_complete(event: cdp_tracing.TracingComplete):
        done_event.set()

    tab.add_handler(cdp_tracing.TracingComplete, on_complete)
    await tab.send(cdp_tracing.end())

    try:
        await asyncio.wait_for(done_event.wait(), timeout=30)
    except asyncio.TimeoutError:
        pass

    _tracing_active = False
    tab.remove_handler(cdp_tracing.DataCollected, on_data)
    tab.remove_handler(cdp_tracing.TracingComplete, on_complete)

    if file_path and trace_chunks:
        import json as json_mod
        with open(file_path, "w") as f:
            json_mod.dump(trace_chunks, f)
        return json.dumps({"status": "ok", "events": len(trace_chunks), "saved_to": file_path})

    return json.dumps({"status": "ok", "events": len(trace_chunks)})


@mcp.tool()
async def press_key(key: str) -> str:
    """Press a keyboard key (e.g. Enter, Tab, Escape, ArrowDown).

    Args:
        key: The key name to press.
    """
    tab = await _active_tab()
    import nodriver.cdp.input_ as cdp_input
    await tab.send(cdp_input.dispatch_key_event(type_="keyDown", key=key))
    await tab.send(cdp_input.dispatch_key_event(type_="keyUp", key=key))
    return json.dumps({"status": "pressed", "key": key})


@mcp.tool()
async def resize_page(width: int = 1280, height: int = 720) -> str:
    """Resize the browser window.

    Args:
        width: Window width in pixels.
        height: Window height in pixels.
    """
    tab = await _active_tab()
    await tab.set_window_size(width=width, height=height)
    return json.dumps({"status": "ok", "size": f"{width}x{height}"})


@mcp.tool()
async def scroll_page(direction: str = "down", amount: int = 50) -> str:
    """Scroll the page up or down.

    Args:
        direction: "up" or "down".
        amount: Percentage of page to scroll (25 = quarter page).
    """
    tab = await _active_tab()
    if direction == "down":
        await tab.scroll_down(amount)
    else:
        await tab.scroll_up(amount)
    return json.dumps({"status": "scrolled", "direction": direction, "amount": amount})


@mcp.tool()
async def select_page(index: int) -> str:
    """Switch to a tab by its index (from list_pages).

    Args:
        index: The tab index to activate.
    """
    browser = await _get_browser()
    if index < 0 or index >= len(browser.tabs):
        return json.dumps({"error": f"Invalid index {index}, have {len(browser.tabs)} tabs"})
    tab = browser.tabs[index]
    await tab.activate()
    await tab
    return json.dumps({"status": "ok", "url": tab.target.url, "title": tab.target.title})


@mcp.tool()
async def set_cookie(name: str, value: str, domain: str, path: str = "/", secure: bool = False) -> str:
    """Set a browser cookie.

    Args:
        name: Cookie name.
        value: Cookie value.
        domain: Cookie domain.
        path: Cookie path.
        secure: Whether the cookie is secure-only.
    """
    tab = await _active_tab()
    import nodriver.cdp.network as cdp_net
    success = await tab.send(cdp_net.set_cookie(
        name=name, value=value, domain=domain, path=path, secure=secure,
    ))
    return json.dumps({"status": "ok" if success else "failed"})


@mcp.tool()
async def set_local_storage(items: dict[str, str]) -> str:
    """Set localStorage items on the current page.

    Args:
        items: Dict of key-value pairs to set in localStorage.
    """
    tab = await _active_tab()
    await tab.set_local_storage(items)
    return json.dumps({"status": "ok"})


@mcp.tool()
async def take_memory_snapshot(file_path: str) -> str:
    """Capture a heap snapshot for memory leak debugging.

    Args:
        file_path: Path to save the .heapsnapshot file.
    """
    tab = await _active_tab()
    import nodriver.cdp.heap_profiler as cdp_heap

    chunks = []

    async def on_chunk(event: cdp_heap.AddHeapSnapshotChunk):
        chunks.append(event.chunk)

    tab.add_handler(cdp_heap.AddHeapSnapshotChunk, on_chunk)
    await tab.send(cdp_heap.take_heap_snapshot(report_progress=False))
    tab.remove_handler(cdp_heap.AddHeapSnapshotChunk, on_chunk)

    with open(file_path, "w") as f:
        f.write("".join(chunks))

    return json.dumps({"status": "ok", "file": file_path, "size_mb": round(len("".join(chunks)) / 1024 / 1024, 2)})


@mcp.tool()
async def take_screenshot(full_page: bool = False, format: str = "jpeg") -> str:
    """Take a screenshot of the page or element.

    WARNING: Do NOT use this tool to read page content. Use take_snapshot instead
    which returns searchable HTML text. Only use take_screenshot when you
    specifically need a visual image (layout checks, visual regression, etc.).

    Args:
        full_page: If True, capture the entire page (not just viewport).
        format: Image format, "jpeg" or "png".
    """
    tab = await _active_tab()
    import nodriver.cdp.page as cdp_page

    result = await tab.send(cdp_page.capture_screenshot(
        format_=format,
        capture_beyond_viewport=full_page,
    ))
    return json.dumps({
        "status": "ok",
        "format": format,
        "data_base64": result,
    })


@mcp.tool()
async def take_snapshot(verbose: bool = False) -> str:
    """Get the current page's DOM snapshot based on the accessibility tree.
    The snapshot lists page elements along with a unique identifier (uid).
    Always use the latest snapshot. Prefer taking a snapshot over taking a
    screenshot. Returns searchable, structured text that is much smaller
    than an image or raw HTML.

    Args:
        verbose: Whether to include all elements (including ignored/hidden ones). Default is false.
    """
    tab = await _active_tab()
    import nodriver.cdp.accessibility as cdp_a11y

    nodes = await tab.send(cdp_a11y.get_full_ax_tree())

    # Build a lookup: node_id -> AXNode
    node_map: dict[str, Any] = {}
    for node in nodes:
        node_map[node.node_id] = node

    # Build tree structure
    children_map: dict[str, list[str]] = {}
    root_ids: list[str] = []
    nodes_with_parent: set[str] = set()
    for node in nodes:
        if node.child_ids:
            children_map[node.node_id] = list(node.child_ids)
            for cid in node.child_ids:
                nodes_with_parent.add(cid)

    for node in nodes:
        if node.node_id not in nodes_with_parent:
            root_ids.append(node.node_id)

    # Assign stable short uids
    uid_counter = 0
    uid_map: dict[str, str] = {}
    for node in nodes:
        uid_map[node.node_id] = str(uid_counter)
        uid_counter += 1

    def _format_node(node_id: str, depth: int) -> str:
        node = node_map.get(node_id)
        if node is None:
            return ""

        # Skip ignored nodes in non-verbose mode
        if not verbose and node.ignored:
            # Still recurse into children
            parts = []
            for cid in children_map.get(node_id, []):
                parts.append(_format_node(cid, depth))
            return "".join(parts)

        role = ""
        if node.role and node.role.value:
            role = str(node.role.value)

        name = ""
        if node.name and node.name.value:
            name = str(node.name.value)

        value = ""
        if node.value and node.value.value:
            value = str(node.value.value)

        # Collect interesting properties
        props = []
        if node.properties:
            for prop in node.properties:
                pname = prop.name.value if hasattr(prop.name, "value") else str(prop.name)
                pval = prop.value.value if prop.value and prop.value.value is not None else None
                if pname in ("url",):
                    props.append(f'{pname}="{pval}"')
                elif pname in ("focused", "disabled", "expanded", "selected",
                               "checked", "pressed", "required", "modal"):
                    if pval is True or pval == "true":
                        props.append(pname)
                elif pname in ("level",) and pval is not None:
                    props.append(f'{pname}={pval}')

        uid = uid_map.get(node_id, "?")
        indent = "  " * depth
        parts = [f"uid={uid}"]
        if role and role != "none":
            parts.append(role)
        elif role == "none" and verbose:
            parts.append("ignored")
        if name:
            parts.append(f'"{name}"')
        if value and value != name:
            parts.append(f'value="{value}"')
        parts.extend(props)

        line = f"{indent}{' '.join(parts)}\n"

        child_lines = []
        for cid in children_map.get(node_id, []):
            child_lines.append(_format_node(cid, depth + 1))

        return line + "".join(child_lines)

    output_parts = []
    for rid in root_ids:
        output_parts.append(_format_node(rid, 0))
    snapshot_text = "".join(output_parts)

    # Truncate if extremely large
    if len(snapshot_text) > 200_000:
        snapshot_text = snapshot_text[:200_000] + "\n... (truncated)"

    return json.dumps({"status": "ok", "length": len(snapshot_text), "snapshot": snapshot_text})


@mcp.tool()
async def type_text(text: str) -> str:
    """Type text using keyboard input (sends to the currently focused element).

    Args:
        text: The text to type.
    """
    tab = await _active_tab()
    import nodriver.cdp.input_ as cdp_input
    for char in text:
        await tab.send(cdp_input.dispatch_key_event(type_="keyDown", text=char))
        await tab.send(cdp_input.dispatch_key_event(type_="keyUp", text=char))
    return json.dumps({"status": "typed", "length": len(text)})


@mcp.tool()
async def upload_file(selector: str, file_paths: list[str]) -> str:
    """Upload file(s) through a file input element.

    Args:
        selector: CSS selector of the file input element.
        file_paths: List of local file paths to upload.
    """
    tab = await _active_tab()
    import nodriver.cdp.dom as cdp_dom
    elem = await tab.select(selector)
    node_id = elem.node_id
    if not node_id:
        doc = await tab.send(cdp_dom.get_document())
        result = await tab.send(cdp_dom.query_selector(doc.node_id, selector))
        node_id = result
    await tab.send(cdp_dom.set_file_input_files(files=file_paths, node_id=node_id))
    return json.dumps({"status": "uploaded", "files": file_paths})


@mcp.tool()
async def wait_for(selector: str = "", text: str = "", timeout: int = 10) -> str:
    """Wait for an element to appear on the page.

    Args:
        selector: CSS selector to wait for.
        text: Text content to wait for.
        timeout: Maximum seconds to wait.
    """
    tab = await _active_tab()
    try:
        if selector:
            elem = await tab.select(selector, timeout=timeout)
        elif text:
            elem = await tab.find(text, timeout=timeout)
        else:
            return json.dumps({"error": "Provide either selector or text"})
        return json.dumps({"status": "found", "tag": elem.tag_name, "text": elem.text[:200] if elem.text else ""})
    except asyncio.TimeoutError:
        return json.dumps({"status": "timeout", "message": f"Element not found within {timeout}s"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the MCP server via stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
