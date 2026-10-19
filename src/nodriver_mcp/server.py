"""
nodriver-mcp: An undetected Chrome automation MCP server.

Uses nodriver (successor of undetected-chromedriver) as the browser backend,
providing the same MCP tool interface as chrome-devtools-mcp but without
exposing CDP/WebDriver fingerprints that get detected by anti-bot systems.
"""

import asyncio
import base64
import dataclasses
import itertools
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import nodriver as uc
from mcp.server.fastmcp import FastMCP, Image

from .protocol import json_value

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("nodriver-mcp")


# ---------------------------------------------------------------------------
# Global browser state
# ---------------------------------------------------------------------------
_browser: uc.Browser | None = None
_browser_lock = asyncio.Lock()


async def _get_browser() -> uc.Browser:
    """Start the browser on first tool call (lazy init, protected by mutex)."""
    global _browser, _selected_target_id
    async with _browser_lock:
        if _browser is None or _browser.stopped:
            headless = os.environ.get("NODRIVER_HEADLESS", "").lower() in (
                "1",
                "true",
                "yes",
            )
            user_data_dir = os.environ.get(
                "NODRIVER_USER_DATA_DIR",
                os.path.join(
                    os.path.expanduser("~"), ".nodriver-mcp", "chrome-profile"
                ),
            )
            browser_path = os.environ.get("NODRIVER_BROWSER_PATH", None)
            proxy = os.environ.get("NODRIVER_PROXY", None)

            os.makedirs(user_data_dir, exist_ok=True)
            kwargs: dict[str, Any] = {
                "headless": headless,
                "user_data_dir": user_data_dir,
            }
            logger.info("Using user data dir: %s", user_data_dir)
            if browser_path:
                kwargs["browser_executable_path"] = browser_path

            browser_args = json.loads(os.environ.get("NODRIVER_BROWSER_ARGS", "[]"))
            if not isinstance(browser_args, list) or not all(
                isinstance(arg, str) for arg in browser_args
            ):
                raise ValueError(
                    "NODRIVER_BROWSER_ARGS must be a JSON array of strings."
                )
            if proxy:
                browser_args.append(f"--proxy-server={proxy}")
                logger.info("Proxy configured: %s", proxy)
            if browser_args:
                kwargs["browser_args"] = browser_args

            if _browser is not None:
                await _shutdown_browser()
            config = uc.Config(**kwargs)
            extensions = json.loads(os.environ.get("NODRIVER_EXTENSIONS", "[]"))
            if not isinstance(extensions, list) or not all(
                isinstance(path, str) for path in extensions
            ):
                raise ValueError("NODRIVER_EXTENSIONS must be a JSON array of paths.")
            for extension in extensions:
                config.add_extension(extension)
            _browser = await uc.start(config=config)
            _selected_target_id = str(_browser.main_tab.target_id)

            logger.info("Browser started (headless=%s)", headless)

            # Auto-enable network collection on the first tab.
            # Console collection is opt-in because Runtime.enable() can be detected.
            await _auto_enable_network_collection(_browser.main_tab)
    return _browser


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _lifespan(_server):
    try:
        yield
    finally:
        await _shutdown_browser()


mcp = FastMCP(
    "nodriver-mcp",
    lifespan=_lifespan,
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


async def _page_tab() -> uc.Tab:
    """Return the explicitly selected page, refreshing official target discovery."""
    global _selected_target_id
    browser = await _get_browser()
    await browser.update_targets()
    for tab in browser.tabs:
        if str(tab.target_id) == _selected_target_id:
            return tab
    if not browser.tabs:
        raise ValueError("No open pages. Use new_page first.")
    tab = browser.tabs[-1]
    _selected_target_id = str(tab.target_id)
    return tab


async def _active_tab() -> uc.Tab:
    page = await _page_tab()
    frame_id = _selected_frames.get(str(page.target_id))
    return _frame_connections.get((str(page.target_id), frame_id), page)


# ---------------------------------------------------------------------------
# Shared state for console / network collection
# ---------------------------------------------------------------------------
_console_messages: list[dict] = []
_network_requests: list[dict] = []
_preserved_console_messages: list[list[dict]] = []  # last 3 navigations
_preserved_network_requests: list[list[dict]] = []  # last 3 navigations
_tracing_active = False
_network_collection_enabled_tabs: set[str] = set()
_console_collection_enabled_tabs: set[str] = set()
_named_browser_contexts: dict[
    str, Any
] = {}  # isolated_context name -> BrowserContextID
_selected_target_id: str | None = None
_selected_frames: dict[str, str] = {}
_frame_connections: dict[tuple[str, str], uc.Tab] = {}
_console_callbacks: dict[str, Any] = {}
_network_callbacks: dict[str, list[tuple[type, Any]]] = {}
_uid_owners: dict[str, tuple[str, uc.Tab, str]] = {}
_request_ids = itertools.count()
_message_ids = itertools.count()
_trace_tab: uc.Tab | None = None
_shutdown_callbacks: list[Any] = []


def _page_key(tab: uc.Tab) -> str:
    current = tab
    while getattr(current, "parent", None) is not None:
        parent = current.parent
        if isinstance(parent, uc.Browser):
            break
        current = parent
    return str(current.target_id)


def _tab_key(tab: uc.Tab) -> str:
    return str(tab.target_id)


async def _shutdown_browser() -> None:
    global _browser, _selected_target_id, _tracing_active, _trace_tab
    for callback in _shutdown_callbacks:
        await callback()
    browser, _browser = _browser, None
    connections = list(_frame_connections.values())
    if browser is not None:
        connections.extend(browser.targets)
        for connection in {id(c): c for c in connections}.values():
            try:
                await connection.aclose()
            except Exception:
                logger.debug("Connection cleanup failed", exc_info=True)
        await browser.aclose()
        process = browser._process
        browser.stop()
        if process is not None and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        uc.util.get_registered_instances().discard(browser)
    for state in (
        _network_collection_enabled_tabs,
        _console_collection_enabled_tabs,
        _named_browser_contexts,
        _selected_frames,
        _frame_connections,
        _console_callbacks,
        _network_callbacks,
        _uid_owners,
        _uid_to_backend_node_id,
        _unique_id_to_mcp_id,
        _network_requests,
        _console_messages,
        _preserved_network_requests,
        _preserved_console_messages,
    ):
        state.clear()
    _selected_target_id = None
    _tracing_active = False
    _trace_tab = None


async def _evaluate(tab: uc.Tab, expression: str, await_promise: bool = True) -> Any:
    frame_id = _selected_frames.get(_page_key(tab))
    if frame_id and tab is await _page_tab():
        context = await tab.send(
            uc.cdp.page.create_isolated_world(
                uc.cdp.page.FrameId(frame_id), world_name="nodriver-mcp"
            )
        )
        result, error = await tab.send(
            uc.cdp.runtime.evaluate(
                expression,
                context_id=context,
                await_promise=await_promise,
                return_by_value=True,
            )
        )
        if error:
            raise uc.ProtocolException(error)
        return result.value
    # The high-level helper always requests deep serialization. MCP script
    # results need ordinary JSON, including false/zero, rather than a deep graph.
    result, error = await tab.send(
        uc.cdp.runtime.evaluate(
            expression,
            await_promise=await_promise,
            return_by_value=True,
            user_gesture=True,
        )
    )
    if error:
        raise uc.ProtocolException(error)
    return result.value


async def _tab_for_uid(uid: str) -> uc.Tab:
    owner = _uid_owners.get(uid)
    page = await _page_tab()
    if owner is None or owner[0] != str(page.target_id):
        raise ValueError(
            f"Unknown or stale uid '{uid}' for this page. Take a new snapshot."
        )
    return owner[1]


async def _element_for_uid(uid: str) -> uc.Element:
    tab = await _tab_for_uid(uid)
    node = await tab.send(
        uc.cdp.dom.describe_node(
            backend_node_id=uc.cdp.dom.BackendNodeId(_uid_to_backend_node_id[uid]),
            depth=1,
        )
    )
    return uc.Element(node, tab)


class _CookieScope:
    """Give the official CookieJar the selected context's page connection."""

    def __init__(self, tab, browser):
        self.tab = tab
        self.browser = browser

    @property
    def target(self):
        return self.tab.target

    def __iter__(self):
        return iter([self])

    async def send(self, command):
        context_id = self.target.browser_context_id
        if context_id is None:
            return await self.tab.send(command)
        # Storage commands default to the browser's default context even when
        # sent on an isolated page session. Reuse the generated context argument.
        message = next(command)
        command.close()
        storage = uc.cdp.storage
        if message["method"] == "Storage.getCookies":
            scoped = storage.get_cookies(browser_context_id=context_id)
        elif message["method"] == "Storage.setCookies":
            scoped = storage.set_cookies(
                [_cookie_param(c) for c in message["params"]["cookies"]],
                browser_context_id=context_id,
            )
        elif message["method"] == "Storage.clearCookies":
            scoped = storage.clear_cookies(browser_context_id=context_id)
        else:
            raise ValueError(f"Unsupported CookieJar command: {message['method']}")
        return await self.browser.send(scoped)


async def _cookie_jar():
    from nodriver.core.browser import CookieJar

    return CookieJar(_CookieScope(await _page_tab(), await _get_browser()))


async def _discover_frames(page: uc.Tab) -> list[tuple[Any, uc.Tab]]:
    """Combine official get_frames connections with the complete CDP frame tree."""
    root_id = str(page.target_id)
    tree = await page.get_frame_tree()
    native = await page.get_frames()
    by_id = {str(connection.target_id): connection for connection in native}
    result = []
    for frame in uc.util.flatten_frame_tree(tree):
        key = (root_id, str(frame.id_))
        connection = _frame_connections.get(key)
        if connection is None:
            connection = by_id.get(str(frame.id_), page)
            _frame_connections[key] = connection
        result.append((frame, connection))
    known = {str(frame.id_) for frame, _ in result}
    for connection in native:
        if str(connection.target_id) not in known:
            frame = (await connection.get_frame_tree()).frame
            key = (root_id, str(frame.id_))
            cached = _frame_connections.setdefault(key, connection)
            result.append((frame, cached))
    return result


async def _frame_element(page: uc.Tab, frame_id: str) -> uc.Element:
    backend_id, _ = await page.send(
        uc.cdp.dom.get_frame_owner(uc.cdp.page.FrameId(frame_id))
    )
    node = await page.send(
        uc.cdp.dom.describe_node(backend_node_id=backend_id, depth=-1, pierce=True)
    )
    if node.content_document is None:
        raise ValueError("This frame needs a separate connection; refresh list_frames.")
    return uc.Element(node, page, tree=node)


def _cookie_param(data: dict) -> Any:
    from .protocol import wire_name

    payload = {wire_name(key): value for key, value in data.items()}
    if payload.get("expires") is not None and payload["expires"] <= 0:
        payload.pop("expires")
    return uc.cdp.network.CookieParam.from_json(payload)


async def _read_stream(tab, handle) -> bytes:
    chunks = []
    try:
        while True:
            encoded, data, eof = await tab.send(uc.cdp.io.read(handle, size=65536))
            chunks.append(base64.b64decode(data) if encoded else data.encode("utf-8"))
            if eof:
                break
    finally:
        await tab.send(uc.cdp.io.close(handle))
    return b"".join(chunks)


async def _navigate_url(tab: uc.Tab, url: str) -> None:
    # Tab.get() in 0.50.6 attaches a second session after navigating. That leaves
    # Network enabled on the previous session and makes response-body lookup fail.
    # Use the official generated command on the existing native connection.
    result = await tab.send(uc.cdp.page.navigate(url))
    if len(result) > 2 and result[2]:
        raise ValueError(result[2])


_DEVICE_PRESETS: dict[str, dict[str, Any]] = {
    "pixel_7": {
        "aliases": ["pixel7", "android", "android_phone"],
        "viewport": "412x915x2.625,mobile,touch",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/135.0.0.0 Mobile Safari/537.36"
        ),
        "platform": "Android",
        "accept_language": "zh-CN,zh;q=0.9,en;q=0.8",
        "metadata": {
            "platform": "Android",
            "platform_version": "14",
            "architecture": "arm",
            "model": "Pixel 7",
            "mobile": True,
            "form_factors": ["Mobile"],
        },
    },
    "pixel_7_landscape": {
        "aliases": ["pixel7_landscape", "android_landscape"],
        "viewport": "915x412x2.625,mobile,touch,landscape",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/135.0.0.0 Mobile Safari/537.36"
        ),
        "platform": "Android",
        "accept_language": "zh-CN,zh;q=0.9,en;q=0.8",
        "metadata": {
            "platform": "Android",
            "platform_version": "14",
            "architecture": "arm",
            "model": "Pixel 7",
            "mobile": True,
            "form_factors": ["Mobile"],
        },
    },
    "ipad_air": {
        "aliases": ["ipadair", "tablet", "ipad"],
        "viewport": "820x1180x2,mobile,touch",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; Pixel Tablet) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
        ),
        "platform": "Android",
        "accept_language": "zh-CN,zh;q=0.9,en;q=0.8",
        "metadata": {
            "platform": "Android",
            "platform_version": "14",
            "architecture": "arm",
            "model": "Pixel Tablet",
            "mobile": False,
            "form_factors": ["Tablet"],
        },
    },
}

# Stable uid state for take_snapshot (mirrors chrome-devtools-mcp)
_snapshot_id: int = 0
_unique_id_to_mcp_id: dict[str, str] = {}  # "frameId_backendNodeId" -> stable uid
_uid_to_backend_node_id: dict[
    str, int
] = {}  # mcp uid -> backend_dom_node_id (for element resolution)

# Boolean property mapping (same as chrome-devtools-mcp SnapshotFormatter)
_BOOL_PROPERTY_MAP: dict[str, str] = {
    "disabled": "disableable",
    "expanded": "expandable",
    "focused": "focusable",
    "selected": "selectable",
}

# Properties already rendered in the main line or internal-only
_EXCLUDED_PROPERTIES: set[str] = {"role", "name", "children", "elementHandle"}

# Properties Puppeteer doesn't expose — suppress to match chrome-devtools-mcp output
_SUPPRESS_PROPERTIES: set[str] = {
    "focusable",
    "editable",
    "settable",
    "busy",
    "live",
    "relevant",
    "atomic",
    "hidden",
    "controls",
    "describedby",
    "details",
    "errormessage",
    "flowto",
    "labelledby",
    "owns",
    "activedescendant",
}

# Roles to skip entirely (node AND all descendants) — Chrome internals
_SKIP_ROLES: set[str] = {"InlineTextBox", "ListMarker"}

# Roles to collapse (skip node, promote children) — container noise
_COLLAPSE_ROLES: set[str] = {
    "generic",
    "list",
    "listitem",
    "paragraph",
    "strong",
    "emphasis",
    "code",
    "group",
    "Section",
    "blockquote",
    "figure",
    "mark",
    "subscript",
    "superscript",
    "insertion",
    "deletion",
    "DescriptionList",
    "DescriptionListTerm",
    "DescriptionListDetail",
    "time",
    "Abbr",
    "Ruby",
    "RubyAnnotation",
    "term",
    "definition",
    "feed",
    "log",
    "marquee",
    "timer",
    "directory",
    "tooltip",
}

# Modifier keys for press_key combo support
_MODIFIER_KEYS = {"Control", "Shift", "Alt", "Meta"}


def _preserve_on_navigation(tab: uc.Tab) -> None:
    page_id = _page_key(tab)
    for current, history in [
        (_console_messages, _preserved_console_messages),
        (_network_requests, _preserved_network_requests),
    ]:
        batch = [item for item in current if item["page_id"] == page_id]
        if batch:
            history.append(batch)
            del history[:-3]
            current[:] = [item for item in current if item["page_id"] != page_id]
    for uid, owner in list(_uid_owners.items()):
        if owner[0] == page_id:
            _uid_owners.pop(uid, None)
            _uid_to_backend_node_id.pop(uid, None)
    _selected_frames.pop(page_id, None)
    for key, connection in list(_frame_connections.items()):
        if key[0] == page_id:
            if connection is not tab:
                asyncio.create_task(connection.aclose())
            del _frame_connections[key]


# ---------------------------------------------------------------------------
# Auto-collection for network (console collection is explicit)
# ---------------------------------------------------------------------------
async def _auto_enable_network_collection(tab: uc.Tab) -> None:
    """Collect typed network events on their originating connection."""
    key = _tab_key(tab)
    if key in _network_collection_enabled_tabs:
        return
    net = uc.cdp.network

    def request(event):
        _network_requests.append(
            {
                "reqid": next(_request_ids),
                "id": str(event.request_id),
                "page_id": _page_key(tab),
                "connection": tab,
                "url": event.request.url,
                "method": event.request.method,
                "headers": json_value(event.request.headers),
                "post_data": event.request.post_data,
                "timestamp": float(event.timestamp),
                "type": event.type_.value if event.type_ else "unknown",
                "finished": False,
            }
        )
        del _network_requests[:-1000]

    def find(request_id):
        return next(
            (
                r
                for r in reversed(_network_requests)
                if r["connection"] is tab and r["id"] == str(request_id)
            ),
            None,
        )

    def response(event):
        record = find(event.request_id)
        if record is not None:
            record.update(
                status=event.response.status,
                response_headers=json_value(event.response.headers),
                mime_type=event.response.mime_type,
            )

    def finished(event):
        record = find(event.request_id)
        if record is not None:
            record.update(finished=True, encoded_data_length=event.encoded_data_length)

    def failed(event):
        record = find(event.request_id)
        if record is not None:
            record.update(finished=True, error=event.error_text)

    callbacks = [
        (net.RequestWillBeSent, request),
        (net.ResponseReceived, response),
        (net.LoadingFinished, finished),
        (net.LoadingFailed, failed),
    ]
    for event, callback in callbacks:
        tab.add_handler(event, callback)
    try:
        await tab.send(net.enable())
    except BaseException:
        for event, callback in callbacks:
            tab.remove_handler(event, callback)
        raise
    _network_callbacks[key] = callbacks
    _network_collection_enabled_tabs.add(key)


async def _enable_console_collection(tab: uc.Tab) -> bool:
    key = _tab_key(tab)
    if key in _console_collection_enabled_tabs:
        return False

    def on_console(event):
        parts = [
            str(a.value if a.value is not None else a.description or a.type_)
            for a in event.args
        ]
        _console_messages.append(
            {
                "msgid": next(_message_ids),
                "page_id": _page_key(tab),
                "type": event.type_,
                "text": " ".join(parts),
                "timestamp": float(event.timestamp),
            }
        )
        del _console_messages[:-1000]

    tab.add_handler(uc.cdp.runtime.ConsoleAPICalled, on_console)
    try:
        await tab.send(uc.cdp.runtime.enable())
    except BaseException:
        tab.remove_handler(uc.cdp.runtime.ConsoleAPICalled, on_console)
        raise
    _console_callbacks[key] = on_console
    _console_collection_enabled_tabs.add(key)
    return True


async def _disable_console_collection(tab: uc.Tab) -> bool:
    key = _tab_key(tab)
    callback = _console_callbacks.pop(key, None)
    if callback is None:
        return False
    tab.remove_handler(uc.cdp.runtime.ConsoleAPICalled, callback)
    _console_collection_enabled_tabs.discard(key)
    if not any(
        callbacks
        for event, callbacks in tab.handlers.items()
        if event.__module__ == uc.cdp.runtime.__name__
    ):
        await tab.send(uc.cdp.runtime.disable())
    return True


# ---------------------------------------------------------------------------
# UID resolution: uid -> DOM element operations
# ---------------------------------------------------------------------------
async def _resolve_uid(tab: uc.Tab, uid: str) -> Any:
    connection = await _tab_for_uid(uid)
    result = await connection.send(
        uc.cdp.dom.resolve_node(
            backend_node_id=uc.cdp.dom.BackendNodeId(_uid_to_backend_node_id[uid])
        )
    )
    if result is None:
        raise ValueError(f"Could not resolve uid '{uid}'. Take a new snapshot.")
    return result


async def _get_box_model(tab: uc.Tab, uid: str) -> tuple[float, float]:
    connection = await _tab_for_uid(uid)
    backend = uc.cdp.dom.BackendNodeId(_uid_to_backend_node_id[uid])
    await connection.send(
        uc.cdp.dom.scroll_into_view_if_needed(backend_node_id=backend)
    )
    model = await connection.send(uc.cdp.dom.get_box_model(backend_node_id=backend))
    quad = model.content
    return sum(quad[::2]) / 4, sum(quad[1::2]) / 4


async def _maybe_snapshot(include_snapshot: bool) -> str:
    """Optionally append a snapshot to the response."""
    if include_snapshot:
        snapshot = await take_snapshot()
        return "\n\n" + snapshot
    return ""


def _timeout_seconds(timeout_ms: int) -> float | None:
    """Convert MCP timeout milliseconds to asyncio seconds."""
    return timeout_ms / 1000 if timeout_ms and timeout_ms > 0 else None


async def _await_with_timeout(awaitable: Any, timeout_ms: int, action: str) -> Any:
    """Await an operation with an optional MCP-style timeout."""
    timeout_s = _timeout_seconds(timeout_ms)
    try:
        if timeout_s is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"{action} timed out after {timeout_ms}ms") from exc


async def _wait_for_target(
    browser: uc.Browser, target_id: Any, timeout_ms: int
) -> uc.Tab:
    limit = asyncio.get_running_loop().time() + (_timeout_seconds(timeout_ms) or 10)
    while asyncio.get_running_loop().time() < limit:
        await browser.update_targets()
        for tab in browser.tabs:
            if tab.target_id == target_id:
                return tab
        await asyncio.sleep(0.05)
    raise TimeoutError("New page did not appear in the browser target inventory.")


def _resolve_device_preset(device: str) -> dict[str, Any] | None:
    """Resolve a device preset by canonical name or alias."""
    normalized = device.strip().lower().replace("-", "_").replace(" ", "_")
    for name, preset in _DEVICE_PRESETS.items():
        aliases = {
            alias.strip().lower().replace("-", "_").replace(" ", "_")
            for alias in {name, *preset.get("aliases", [])}
        }
        if normalized in aliases:
            return {"name": name, **preset}
    return None


def _build_user_agent_metadata(metadata: dict[str, Any]) -> Any:
    """Build CDP user-agent metadata from a plain dict."""
    import nodriver.cdp.emulation as cdp_emu

    return cdp_emu.UserAgentMetadata(
        platform=metadata["platform"],
        platform_version=metadata["platform_version"],
        architecture=metadata["architecture"],
        model=metadata["model"],
        mobile=metadata["mobile"],
        form_factors=metadata.get("form_factors"),
    )


async def _apply_emulation(
    tab: uc.Tab,
    *,
    network_conditions: str = "",
    cpu_throttling_rate: float = 0,
    geolocation: str | None = None,
    user_agent: str | None = None,
    user_agent_platform: str = "",
    user_agent_metadata: Any = None,
    accept_language: str = "",
    color_scheme: str = "",
    viewport: str = "",
) -> list[str]:
    """Apply emulation settings and return a human-readable summary."""
    results = []

    if network_conditions:
        import nodriver.cdp.network as cdp_net

        presets = {
            "offline": {"offline": True, "latency": 0, "download": 0, "upload": 0},
            "slow 3g": {
                "offline": False,
                "latency": 2000,
                "download": 50000,
                "upload": 50000,
            },
            "fast 3g": {
                "offline": False,
                "latency": 563,
                "download": 180000,
                "upload": 84375,
            },
            "slow 4g": {
                "offline": False,
                "latency": 150,
                "download": 400000,
                "upload": 150000,
            },
            "fast 4g": {
                "offline": False,
                "latency": 50,
                "download": 1500000,
                "upload": 750000,
            },
        }
        p = presets.get(network_conditions.lower(), presets.get("fast 3g"))
        await tab.send(
            cdp_net.emulate_network_conditions(
                offline=p["offline"],
                latency=p["latency"],
                download_throughput=p["download"],
                upload_throughput=p["upload"],
            )
        )
        results.append(f"network={network_conditions}")

    if cpu_throttling_rate and cpu_throttling_rate > 1:
        import nodriver.cdp.emulation as cdp_emu

        await tab.send(cdp_emu.set_cpu_throttling_rate(rate=cpu_throttling_rate))
        results.append(f"cpu_throttle={cpu_throttling_rate}x")

    if geolocation is not None:
        import nodriver.cdp.emulation as cdp_emu

        if geolocation:
            parts = geolocation.split(",")
            lat, lng = float(parts[0]), float(parts[1])
            await tab.send(
                cdp_emu.set_geolocation_override(
                    latitude=lat, longitude=lng, accuracy=1.0
                )
            )
            results.append(f"geolocation={lat},{lng}")
        else:
            await tab.send(cdp_emu.clear_geolocation_override())
            results.append("geolocation=reset")

    if user_agent is not None:
        import nodriver.cdp.network as cdp_net

        if user_agent:
            kwargs: dict[str, Any] = {"user_agent": user_agent}
            if accept_language:
                kwargs["accept_language"] = accept_language
            if user_agent_platform:
                kwargs["platform"] = user_agent_platform
            if user_agent_metadata is not None:
                kwargs["user_agent_metadata"] = user_agent_metadata
            await tab.send(cdp_net.set_user_agent_override(**kwargs))
            results.append("user_agent set")
            if user_agent_metadata is not None:
                results.append("ua_client_hints set")
        else:
            await tab.send(cdp_net.set_user_agent_override(user_agent=""))
            results.append("user_agent reset")

    if color_scheme and color_scheme != "auto":
        import nodriver.cdp.emulation as cdp_emu

        await tab.send(
            cdp_emu.set_emulated_media(
                features=[
                    cdp_emu.MediaFeature(
                        name="prefers-color-scheme", value=color_scheme
                    )
                ]
            )
        )
        results.append(f"color_scheme={color_scheme}")
    elif color_scheme == "auto":
        import nodriver.cdp.emulation as cdp_emu

        await tab.send(cdp_emu.set_emulated_media(features=[]))
        results.append("color_scheme=auto (reset)")

    if viewport:
        import nodriver.cdp.emulation as cdp_emu

        parts = viewport.split(",")
        dims = parts[0].split("x")
        w, h = int(dims[0]), int(dims[1])
        dpr = float(dims[2]) if len(dims) > 2 else 1.0
        flags = {f.strip().lower() for f in parts[1:] if f.strip()}
        mobile = "mobile" in flags
        touch = "touch" in flags
        landscape = "landscape" in flags
        orientation = cdp_emu.ScreenOrientation(
            type_="landscapePrimary" if landscape else "portraitPrimary",
            angle=90 if landscape else 0,
        )
        await tab.send(
            cdp_emu.set_device_metrics_override(
                width=w,
                height=h,
                device_scale_factor=dpr,
                mobile=mobile,
                screen_width=w,
                screen_height=h,
                screen_orientation=orientation,
            )
        )
        await tab.send(
            cdp_emu.set_touch_emulation_enabled(
                enabled=touch,
                max_touch_points=5 if touch else None,
            )
        )
        await tab.send(
            cdp_emu.set_emit_touch_events_for_mouse(
                enabled=touch,
                configuration="mobile" if mobile else "desktop",
            )
        )
        results.append(f"viewport={viewport}")
        results.append(f"touch={'on' if touch else 'off'}")

    return results


async def _apply_device_preset(tab: uc.Tab, device: str) -> list[str]:
    """Apply a named device preset to a tab."""
    resolved = _resolve_device_preset(device)
    if resolved is None:
        supported = ", ".join(sorted(_DEVICE_PRESETS))
        raise ValueError(
            f"Unknown device preset '{device}'. Supported presets: {supported}"
        )

    metadata = _build_user_agent_metadata(resolved["metadata"])
    results = await _apply_emulation(
        tab,
        user_agent=resolved["user_agent"],
        user_agent_platform=resolved["platform"],
        user_agent_metadata=metadata,
        accept_language=resolved.get("accept_language", ""),
        viewport=resolved["viewport"],
    )
    results.insert(0, f"device={resolved['name']}")
    return results


async def _reset_emulation(tab: uc.Tab) -> list[str]:
    """Reset emulation overrides on the current tab back to browser defaults."""
    import nodriver.cdp.emulation as cdp_emu
    import nodriver.cdp.network as cdp_net

    results = []

    await tab.send(
        cdp_net.emulate_network_conditions(
            offline=False,
            latency=0,
            download_throughput=-1,
            upload_throughput=-1,
        )
    )
    results.append("network=reset")

    await tab.send(cdp_emu.set_cpu_throttling_rate(rate=1))
    results.append("cpu_throttle=reset")

    await tab.send(cdp_emu.clear_geolocation_override())
    results.append("geolocation=reset")

    await tab.send(cdp_net.set_user_agent_override(user_agent=""))
    results.append("user_agent=reset")

    await tab.send(cdp_emu.set_emulated_media(features=[]))
    results.append("color_scheme=reset")

    await tab.send(cdp_emu.clear_device_metrics_override())
    results.append("viewport=reset")

    await tab.send(cdp_emu.reset_page_scale_factor())
    results.append("page_scale=reset")

    await tab.send(cdp_emu.set_touch_emulation_enabled(enabled=False))
    await tab.send(cdp_emu.set_emit_touch_events_for_mouse(enabled=False))
    results.append("touch=reset")

    return results


async def _open_new_tab(
    browser: uc.Browser,
    *,
    url: str,
    background: bool,
    isolated_context: str,
    timeout: int,
) -> uc.Tab:
    if not isolated_context and not background:
        return await _await_with_timeout(
            browser.get(url, new_tab=True), timeout, "Open page"
        )
    ctx = _named_browser_contexts.get(isolated_context) if isolated_context else None
    if isolated_context and ctx is None:
        ctx = await browser.send(
            uc.cdp.target.create_browser_context(dispose_on_detach=True)
        )
        _named_browser_contexts[isolated_context] = ctx
    target = await _await_with_timeout(
        browser.send(
            uc.cdp.target.create_target(
                url=url, browser_context_id=ctx, background=background
            )
        ),
        timeout,
        "Create page",
    )
    return await _wait_for_target(browser, target, timeout)


async def _format_pages() -> str:
    browser = await _get_browser()
    await browser.update_targets()
    return "\nOpen pages:\n" + "\n".join(
        f"  [{i}]{' *' if str(tab.target_id) == _selected_target_id else ''} "
        f"{tab.target.url} — {tab.target.title} (target={tab.target_id})"
        for i, tab in enumerate(browser.tabs)
    )


# ---------------------------------------------------------------------------
# Tools (aligned with chrome-devtools-mcp interface)
# ---------------------------------------------------------------------------


@mcp.tool()
async def bypass_insecure_warning() -> str:
    """Click through the browser's insecure connection warning page."""
    tab = await _active_tab()
    await tab.bypass_insecure_connection_warning()
    return "Bypassed insecure connection warning."


@mcp.tool()
async def cf_verify() -> str:
    """Attempt to solve a Cloudflare verification challenge.

    Uses nodriver's built-in CF verification bypass.
    Requires opencv-python to be installed.
    """
    tab = await _active_tab()
    try:
        await tab.verify_cf()
        return "Cloudflare verification attempted."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def click(
    uid: str, dbl_click: bool = False, include_snapshot: bool = False
) -> str:
    """Click a snapshot element using the official Element helper."""
    try:
        element = await _element_for_uid(uid)
        if dbl_click:
            connection = await _tab_for_uid(uid)
            x, y = await _get_box_model(connection, uid)
            for event in ("mousePressed", "mouseReleased"):
                await connection.send(
                    uc.cdp.input_.dispatch_mouse_event(
                        event,
                        x=x,
                        y=y,
                        button=uc.cdp.input_.MouseButton.LEFT,
                        buttons=1 if event == "mousePressed" else 0,
                        click_count=2,
                    )
                )
        else:
            await element.click()
        return f"Clicked uid={uid}" + await _maybe_snapshot(include_snapshot)
    except Exception as exc:
        return f"Error clicking uid={uid}: {exc}"


@mcp.tool()
async def click_at(
    x: int, y: int, dbl_click: bool = False, include_snapshot: bool = False
) -> str:
    """Click at specific coordinates on the page.

    Args:
        x: The x coordinate.
        y: The y coordinate.
        dbl_click: Set to true for double clicks.
        include_snapshot: Whether to include a snapshot in the response. Default is false.
    """
    tab = await _active_tab()
    await tab.mouse_click(x, y)
    if dbl_click:
        await tab.mouse_click(x, y)
    result = f"Clicked at ({x}, {y})"
    result += await _maybe_snapshot(include_snapshot)
    return result


@mcp.tool()
async def close_page(page_id: int = -1) -> str:
    """Close a page by list_pages index, retaining at least one page."""
    global _selected_target_id
    browser = await _get_browser()
    await browser.update_targets()
    if len(browser.tabs) <= 1:
        return "Error: Cannot close the last open page."
    if page_id == -1:
        tab = await _page_tab()
    elif 0 <= page_id < len(browser.tabs):
        tab = browser.tabs[page_id]
    else:
        return f"Error: Invalid page_id {page_id}."
    key = str(tab.target_id)
    await tab.close()
    await tab.aclose()
    _selected_frames.pop(key, None)
    for pair, connection in list(_frame_connections.items()):
        if pair[0] == key:
            if connection is not tab:
                await connection.aclose()
            del _frame_connections[pair]
    for uid, owner in list(_uid_owners.items()):
        if owner[0] == key:
            _uid_owners.pop(uid, None)
            _uid_to_backend_node_id.pop(uid, None)
    if _selected_target_id == key:
        _selected_target_id = None
    await browser.update_targets()
    return "Page closed." + await _format_pages()


@mcp.tool()
async def drag(from_uid: str, to_uid: str, include_snapshot: bool = False) -> str:
    """Drag an element onto another element.

    Args:
        from_uid: The uid of the element to drag.
        to_uid: The uid of the element to drop into.
        include_snapshot: Whether to include a snapshot in the response. Default is false.
    """
    tab = await _tab_for_uid(from_uid)
    try:
        if await _tab_for_uid(to_uid) is not tab:
            raise ValueError("Drag endpoints must belong to the same frame connection.")
        src_x, src_y = await _get_box_model(tab, from_uid)
        dst_x, dst_y = await _get_box_model(tab, to_uid)
        await tab.mouse_drag((src_x, src_y), (dst_x, dst_y))
        result = f"Dragged uid={from_uid} to uid={to_uid}"
        result += await _maybe_snapshot(include_snapshot)
        return result
    except Exception as e:
        return f"Error dragging: {e}"


@mcp.tool()
async def emulate(
    network_conditions: str = "",
    cpu_throttling_rate: float = 0,
    geolocation: str | None = None,
    user_agent: str | None = None,
    color_scheme: str = "",
    viewport: str = "",
) -> str:
    """Emulates various features on the selected page.

    Args:
        network_conditions: Throttle network. Options: "Offline", "Slow 3G", "Fast 3G", "Slow 4G", "Fast 4G". Omit to disable.
        cpu_throttling_rate: CPU slowdown factor (1-20). Omit or set to 1 to disable.
        geolocation: Geolocation as "latitude,longitude" (e.g. "37.7749,-122.4194"). Omit to leave unchanged, or set to "" to clear.
        user_agent: User agent string. Omit to leave unchanged, or set to "" to clear.
        color_scheme: "dark", "light", or "auto". Empty to skip.
        viewport: Viewport as "widthxheightxdpr[,mobile][,touch][,landscape]" (e.g. "375x812x3,mobile,touch"). Empty to skip.
    """
    tab = await _active_tab()
    results = await _apply_emulation(
        tab,
        network_conditions=network_conditions,
        cpu_throttling_rate=cpu_throttling_rate,
        geolocation=geolocation,
        user_agent=user_agent,
        color_scheme=color_scheme,
        viewport=viewport,
    )

    return (
        "Emulation applied: " + ", ".join(results)
        if results
        else "No emulation changes applied."
    )


@mcp.tool()
async def reset_emulation() -> str:
    """Reset emulation overrides on the selected page back to browser defaults."""
    tab = await _active_tab()
    results = await _reset_emulation(tab)
    return "Emulation reset: " + ", ".join(results)


@mcp.tool()
async def emulate_device(
    device: str,
    color_scheme: str = "",
    network_conditions: str = "",
    cpu_throttling_rate: float = 0,
    geolocation: str | None = None,
) -> str:
    """Apply a named mobile/tablet device preset to the current page.

    Args:
        device: Device preset name or alias. Supported presets: pixel_7, pixel_7_landscape, ipad_air.
        color_scheme: "dark", "light", or "auto". Empty to skip.
        network_conditions: Throttle network. Options: "Offline", "Slow 3G", "Fast 3G", "Slow 4G", "Fast 4G".
        cpu_throttling_rate: CPU slowdown factor (1-20). Omit or set to 1 to disable.
        geolocation: Geolocation as "latitude,longitude" (e.g. "37.7749,-122.4194"). Omit to leave unchanged, or set to "" to clear.
    """
    if _resolve_device_preset(device) is None:
        supported = ", ".join(sorted(_DEVICE_PRESETS))
        return (
            f"Error: Unknown device preset '{device}'. Supported presets: {supported}"
        )

    tab = await _active_tab()
    device_results = await _apply_device_preset(tab, device)
    extra_results = await _apply_emulation(
        tab,
        network_conditions=network_conditions,
        cpu_throttling_rate=cpu_throttling_rate,
        geolocation=geolocation,
        color_scheme=color_scheme,
    )
    return "Emulation applied: " + ", ".join([*device_results, *extra_results])


@mcp.tool()
async def evaluate_script(function: str, args: list[str] | None = None) -> str:
    """Evaluate JavaScript, optionally passing snapshot element uids as arguments."""
    try:
        tab = await _active_tab()
        if args:
            tab = await _tab_for_uid(args[0])
            for uid in args[1:]:
                if await _tab_for_uid(uid) is not tab:
                    raise ValueError(
                        "Element arguments must belong to the same frame connection."
                    )
            objects = [await _resolve_uid(tab, uid) for uid in args]
            result, error = await tab.send(
                uc.cdp.runtime.call_function_on(
                    function_declaration=function,
                    object_id=objects[0].object_id,
                    arguments=[
                        uc.cdp.runtime.CallArgument(object_id=o.object_id)
                        for o in objects
                    ],
                    return_by_value=True,
                    await_promise=True,
                    user_gesture=True,
                )
            )
            if error:
                raise uc.ProtocolException(error)
            value = result.value
        else:
            expression = function.strip()
            if expression.startswith(("(", "function", "async")):
                expression = f"({expression})()"
            value = await _evaluate(tab, expression)
        return "```json\n" + json.dumps(json_value(value), ensure_ascii=False) + "\n```"
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
async def fill(uid: str, value: str, include_snapshot: bool = False) -> str:
    """Fill an input or select using official Element.apply/clear_input/send_keys."""
    try:
        element = await _element_for_uid(uid)
        tag = await element.apply("(el) => el.tagName.toLowerCase()")
        if tag == "select":
            await element.apply(
                "(el) => { el.value = "
                + json.dumps(value)
                + "; el.dispatchEvent(new Event('input', {bubbles:true})); "
                "el.dispatchEvent(new Event('change', {bubbles:true})); }"
            )
        else:
            await element.clear_input()
            await element.send_keys(value)
        return f"Filled uid={uid}" + await _maybe_snapshot(include_snapshot)
    except Exception as exc:
        return f"Error filling uid={uid}: {exc}"


@mcp.tool()
async def fill_form(elements: list[dict], include_snapshot: bool = False) -> str:
    """Fill multiple {uid, value} entries and report individual failures accurately."""
    results = []
    for entry in elements:
        uid = entry.get("uid", "")
        result = await fill(uid, entry.get("value", ""))
        results.append(
            f"  uid={uid}: {result if result.startswith('Error') else 'filled'}"
        )
    return (
        "Form fill results:\n"
        + "\n".join(results)
        + await _maybe_snapshot(include_snapshot)
    )


@mcp.tool()
async def get_console_message(msgid: int) -> str:
    """Get a stable message ID from list_console_messages on the selected page."""
    page_id = _page_key(await _page_tab())
    records = _console_messages + [
        m for batch in _preserved_console_messages for m in batch
    ]
    message = next(
        (m for m in records if m["msgid"] == msgid and m["page_id"] == page_id), None
    )
    if message is None:
        return f"Error: Unknown msgid {msgid} for this page."
    return f"[{message['type']}] {message['text']} (timestamp: {message['timestamp']})"


@mcp.tool()
async def get_cookies(url: str = "") -> str:
    """Get all cookies in the selected context through the official CookieJar."""
    from urllib.parse import urlsplit

    cookies = await (await _cookie_jar()).get_all()
    if url:
        parsed = urlsplit(url)
        host, path = parsed.hostname or "", parsed.path or "/"
        cookies = [
            c
            for c in cookies
            if (
                host == c.domain.lstrip(".")
                or (c.domain.startswith(".") and host.endswith(c.domain))
            )
            and (path == c.path or path.startswith(c.path.rstrip("/") + "/"))
            and (not c.secure or parsed.scheme == "https")
        ]
    return json.dumps(
        [cookie.to_json() for cookie in cookies], ensure_ascii=False, indent=2
    )


@mcp.tool()
async def get_local_storage() -> str:
    """Get all localStorage items from the current page."""
    tab = await _active_tab()
    data = await tab.get_local_storage()
    lines = ["localStorage items:"]
    for k, v in (data or {}).items():
        lines.append(f"  {k}: {str(v)[:200]}")
    return "\n".join(lines)


@mcp.tool()
async def get_network_request(
    reqid: int | None = None, request_file_path: str = "", response_file_path: str = ""
) -> str:
    """Read a stable request ID using its originating page/frame connection."""
    page_id = _page_key(await _page_tab())
    records = [
        r for batch in _preserved_network_requests for r in batch
    ] + _network_requests
    records = [r for r in records if r["page_id"] == page_id]
    req = (
        next((r for r in records if r["reqid"] == reqid), None)
        if reqid is not None
        else next(iter(reversed(records)), None)
    )
    if req is None:
        return "Error: No matching request for this page."
    tab = req["connection"]
    lines = [
        json.dumps(
            {k: v for k, v in req.items() if k != "connection"},
            ensure_ascii=False,
            indent=2,
        )
    ]
    try:
        body = req.get("post_data")
        if body is None:
            body = await tab.send(
                uc.cdp.network.get_request_post_data(
                    uc.cdp.network.RequestId(req["id"])
                )
            )
        if request_file_path:
            Path(request_file_path).write_text(body or "", encoding="utf-8")
        elif body:
            lines.append("Request body: " + body[:5000])
    except uc.ProtocolException:
        pass
    try:
        body, encoded = await tab.send(
            uc.cdp.network.get_response_body(uc.cdp.network.RequestId(req["id"]))
        )
        if response_file_path:
            Path(response_file_path).write_bytes(
                base64.b64decode(body) if encoded else body.encode("utf-8")
            )
            lines.append(f"Response body saved to {response_file_path}")
        else:
            lines.append(
                f"Response body{' (base64)' if encoded else ''}: {body[:5000]}"
            )
    except Exception as exc:
        lines.append(f"Response body unavailable: {exc}")
    return "\n".join(lines)


@mcp.tool()
async def handle_dialog(action: str = "accept", prompt_text: str = "") -> str:
    """If a browser dialog was opened, use this command to handle it.

    Args:
        action: Whether to "accept" or "dismiss" the dialog.
        prompt_text: Optional text to enter into a prompt dialog.
    """
    tab = await _active_tab()
    import nodriver.cdp.page as cdp_page

    if action == "accept":
        await tab.send(
            cdp_page.handle_java_script_dialog(accept=True, prompt_text=prompt_text)
        )
    else:
        await tab.send(cdp_page.handle_java_script_dialog(accept=False))
    return f"Dialog {action}ed."


@mcp.tool()
async def hover(uid: str, include_snapshot: bool = False) -> str:
    """Hover over the provided element.

    Args:
        uid: The uid of an element on the page from the page content snapshot.
        include_snapshot: Whether to include a snapshot in the response. Default is false.
    """
    tab = await _tab_for_uid(uid)
    try:
        cx, cy = await _get_box_model(tab, uid)
        await tab.mouse_move(cx, cy)
        result = f"Hovered over uid={uid}"
        result += await _maybe_snapshot(include_snapshot)
        return result
    except Exception as e:
        return f"Error hovering uid={uid}: {e}"


@mcp.tool()
async def list_console_messages(
    page_size: int | None = None,
    page_idx: int = 0,
    types: list[str] | None = None,
    include_preserved_messages: bool = False,
) -> str:
    """List stable console message IDs for the selected page, with optional filtering."""
    tab = await _active_tab()
    if _tab_key(tab) not in _console_collection_enabled_tabs:
        return "Console collection is disabled. Call enable_console_collection first."
    records = list(_console_messages)
    if include_preserved_messages:
        records = [m for batch in _preserved_console_messages for m in batch] + records
    records = [
        m
        for m in records
        if m["page_id"] == _page_key(tab) and (not types or m["type"] in types)
    ]
    total = len(records)
    if page_size is not None:
        if page_size < 1 or page_idx < 0:
            raise ValueError("page_size must be positive and page_idx nonnegative.")
        records = records[page_idx * page_size : (page_idx + 1) * page_size]
    return f"Console messages ({len(records)} of {total}):\n" + "\n".join(
        f"  [{m['msgid']}] [{m['type']}] {m['text'][:200]}" for m in records
    )


@mcp.tool()
async def enable_console_collection() -> str:
    """Enable console event collection on the current page."""
    tab = await _active_tab()
    changed = await _enable_console_collection(tab)
    if changed:
        return "Console collection enabled on the current page."
    return "Console collection was already enabled on the current page."


@mcp.tool()
async def disable_console_collection() -> str:
    """Disable console event collection on the current page."""
    tab = await _active_tab()
    changed = await _disable_console_collection(tab)
    if changed:
        return "Console collection disabled on the current page."
    return "Console collection was already disabled on the current page."


@mcp.tool()
async def list_network_requests(
    page_size: int | None = None,
    page_idx: int = 0,
    resource_types: list[str] | None = None,
    url_filter: str = "",
    include_preserved_requests: bool = False,
) -> str:
    """List stable request IDs for this page. IDs survive filtering and pagination."""
    page_id = _page_key(await _page_tab())
    records = list(_network_requests)
    if include_preserved_requests:
        records = [r for batch in _preserved_network_requests for r in batch] + records
    allowed = {t.lower() for t in resource_types} if resource_types else None
    records = [
        r
        for r in records
        if r["page_id"] == page_id
        and (allowed is None or r["type"].lower() in allowed)
        and url_filter in r["url"]
    ]
    total = len(records)
    if page_size is not None:
        if page_size < 1 or page_idx < 0:
            raise ValueError("page_size must be positive and page_idx nonnegative.")
        records = records[page_idx * page_size : (page_idx + 1) * page_size]
    return f"Network requests ({len(records)} of {total}):\n" + "\n".join(
        f"  [{r['reqid']}] {r['method']} {r['url'][:150]} ({r['type']}, status={r.get('status', 'pending')})"
        for r in records
    )


@mcp.tool()
async def list_pages() -> str:
    """List page indexes, target IDs, selected page and current URLs."""
    return (await _format_pages()).lstrip()


@mcp.tool()
async def navigate_page(
    type: str = "url",
    url: str = "",
    ignore_cache: bool = False,
    handle_before_unload: str = "accept",
    init_script: str = "",
    timeout: int = 0,
    device: str = "",
) -> str:
    """Navigate the selected page. Optional device emulation runs before navigation."""
    tab = await _page_tab()
    if type not in {"url", "back", "forward", "reload"} or (type == "url" and not url):
        return "Error: Supply a valid navigation type and URL."
    if handle_before_unload not in {"accept", "dismiss"}:
        return "Error: handle_before_unload must be accept or dismiss."
    if device and _resolve_device_preset(device) is None:
        return f"Error: Unknown device preset '{device}'."
    _preserve_on_navigation(tab)
    _selected_frames.pop(str(tab.target_id), None)

    def dialog(event):
        if event.type_.value == "beforeunload":
            asyncio.create_task(
                tab.send(
                    uc.cdp.page.handle_java_script_dialog(
                        accept=handle_before_unload == "accept"
                    )
                )
            )

    tab.add_handler(uc.cdp.page.JavascriptDialogOpening, dialog)
    script_id = None
    try:
        await tab.send(uc.cdp.page.enable())
        if init_script:
            script_id = await tab.send(
                uc.cdp.page.add_script_to_evaluate_on_new_document(init_script)
            )
        if device:
            await _apply_device_preset(tab, device)
        await _auto_enable_network_collection(tab)
        actions = {
            "url": lambda: _navigate_url(tab, url),
            "back": tab.back,
            "forward": tab.forward,
            "reload": lambda: tab.reload(ignore_cache=ignore_cache),
        }
        await _await_with_timeout(actions[type](), timeout, "Navigation")
        await _await_with_timeout(tab.wait(), timeout, "Wait for navigation")
        return f"Navigated to {tab.target.url}" + await _format_pages()
    except Exception as exc:
        return f"Error: {exc}"
    finally:
        tab.remove_handler(uc.cdp.page.JavascriptDialogOpening, dialog)
        if script_id is not None:
            await tab.send(
                uc.cdp.page.remove_script_to_evaluate_on_new_document(script_id)
            )


@mcp.tool()
async def new_page(
    url: str = "about:blank",
    background: bool = False,
    isolated_context: str = "",
    timeout: int = 0,
    device: str = "",
) -> str:
    """Open a page, optionally in a named context; apply device settings before its first request."""
    global _selected_target_id
    if device and _resolve_device_preset(device) is None:
        return f"Error: Unknown device preset '{device}'."
    browser = await _get_browser()
    previous = _selected_target_id
    try:
        tab = await _open_new_tab(
            browser,
            url="about:blank",
            background=background,
            isolated_context=isolated_context,
            timeout=timeout,
        )
        await _auto_enable_network_collection(tab)
        if device:
            await _apply_device_preset(tab, device)
        if url != "about:blank":
            await _await_with_timeout(
                _navigate_url(tab, url), timeout, "Navigate new page"
            )
            await _await_with_timeout(tab.wait(), timeout, "Wait for navigation")
        if not background:
            _selected_target_id = str(tab.target_id)
            await tab.activate()
        else:
            _selected_target_id = previous
        return f"Opened new page: {tab.target.url}" + await _format_pages()
    except Exception as exc:
        return f"Error opening new page: {exc}"


@mcp.tool()
async def performance_start_trace(
    reload: bool = True, auto_stop: bool = True, file_path: str = ""
) -> str:
    """Start an official CDP trace; retain its connection when pages are switched."""
    global _tracing_active, _trace_tab
    if _tracing_active:
        return "Error: A trace is already running."
    tab = await _page_tab()
    await tab.send(
        uc.cdp.tracing.start(
            categories="devtools.timeline,blink.user_timing,v8",
            transfer_mode="ReturnAsStream",
        )
    )
    _tracing_active, _trace_tab = True, tab
    if reload:
        await tab.reload()
    if auto_stop:
        await tab.sleep(5)
        return await performance_stop_trace(file_path)
    return "Trace started. Use performance_stop_trace to stop."


@mcp.tool()
async def performance_stop_trace(file_path: str = "") -> str:
    """Finish the trace and consume its official IO stream."""
    global _tracing_active, _trace_tab
    if not _tracing_active or _trace_tab is None:
        return "Error: No trace is running."
    tab = _trace_tab
    done = asyncio.get_running_loop().create_future()

    def complete(event):
        if not done.done():
            done.set_result(event)

    tab.add_handler(uc.cdp.tracing.TracingComplete, complete)
    try:
        await tab.send(uc.cdp.tracing.end())
        event = await asyncio.wait_for(done, 30)
        if event.stream is None:
            raise ValueError("Chrome did not return a trace stream.")
        data = await _read_stream(tab, event.stream)
        parsed = json.loads(data)
        count = len(parsed.get("traceEvents", []))
        if file_path:
            Path(file_path).write_bytes(data)
        return f"Trace stopped. {count} events collected." + (
            f" Saved to {file_path}" if file_path else ""
        )
    finally:
        tab.remove_handler(uc.cdp.tracing.TracingComplete, complete)
        _tracing_active, _trace_tab = False, None


@mcp.tool()
async def press_key(key: str, include_snapshot: bool = False) -> str:
    """Press a key or key combination.

    Args:
        key: A key or combination (e.g. "Enter", "Control+A", "Control+Shift+R").
            Modifiers: Control, Shift, Alt, Meta.
        include_snapshot: Whether to include a snapshot in the response. Default is false.
    """
    tab = await _active_tab()
    import nodriver.cdp.input_ as cdp_input

    parts = key.split("+")
    modifiers = []
    target_key = parts[-1]

    for p in parts[:-1]:
        if p in _MODIFIER_KEYS:
            modifiers.append(p)

    # Press modifiers down
    for mod in modifiers:
        await tab.send(cdp_input.dispatch_key_event(type_="keyDown", key=mod))

    # Press the target key
    await tab.send(cdp_input.dispatch_key_event(type_="keyDown", key=target_key))
    await tab.send(cdp_input.dispatch_key_event(type_="keyUp", key=target_key))

    # Release modifiers in reverse
    for mod in reversed(modifiers):
        await tab.send(cdp_input.dispatch_key_event(type_="keyUp", key=mod))

    result = f"Pressed {key}"
    result += await _maybe_snapshot(include_snapshot)
    return result


@mcp.tool()
async def resize_page(width: int, height: int) -> str:
    """Resizes the selected page's window so that the page has specified dimension.

    Args:
        width: Page width in pixels.
        height: Page height in pixels.
    """
    tab = await _active_tab()
    await tab.set_window_size(width=width, height=height)
    return f"Resized to {width}x{height}."


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
    return f"Scrolled {direction} {amount}%."


@mcp.tool()
async def select_page(page_id: int, bring_to_front: bool = True) -> str:
    """Select a page as the persistent context for subsequent tools."""
    global _selected_target_id
    browser = await _get_browser()
    await browser.update_targets()
    if not 0 <= page_id < len(browser.tabs):
        return f"Error: Invalid page_id {page_id}."
    tab = browser.tabs[page_id]
    _selected_target_id = str(tab.target_id)
    _selected_frames.pop(_selected_target_id, None)
    await _auto_enable_network_collection(tab)
    if bring_to_front:
        await tab.activate()
    return f"Selected page [{page_id}]: {tab.target.url}" + await _format_pages()


@mcp.tool()
async def set_cookie(
    name: str, value: str, domain: str, path: str = "/", secure: bool = False
) -> str:
    """Set a cookie through the official typed CookieJar.set_all API."""
    jar = await _cookie_jar()
    await jar.set_all(
        [
            _cookie_param(
                dict(name=name, value=value, domain=domain, path=path, secure=secure)
            )
        ]
    )
    return f"Cookie '{name}' set."


@mcp.tool()
async def set_local_storage(items: dict[str, str]) -> str:
    """Set localStorage items on the current page.

    Args:
        items: Dict of key-value pairs to set in localStorage.
    """
    tab = await _active_tab()
    await tab.set_local_storage(items)
    return f"Set {len(items)} localStorage items."


@mcp.tool()
async def take_memory_snapshot(file_path: str) -> str:
    """Save an official heap snapshot, removing its listener even on failure."""
    tab = await _active_tab()
    chunks = []

    def on_chunk(event):
        chunks.append(event.chunk)

    tab.add_handler(uc.cdp.heap_profiler.AddHeapSnapshotChunk, on_chunk)
    try:
        await tab.send(uc.cdp.heap_profiler.enable())
        await tab.send(uc.cdp.heap_profiler.take_heap_snapshot(report_progress=False))
        data = "".join(chunks)
        Path(file_path).write_text(data, encoding="utf-8")
        return f"Heap snapshot saved to {file_path} ({len(data)} characters)."
    finally:
        tab.remove_handler(uc.cdp.heap_profiler.AddHeapSnapshotChunk, on_chunk)


@mcp.tool()
async def take_screenshot(
    full_page: bool = False,
    format: str = "png",
    quality: int = 0,
    uid: str = "",
    file_path: str = "",
) -> Any:
    """Capture an MCP image or save a file. Ordinary PNG/JPEG uses native as_base64.

    Element crops, WebP and explicit quality use official CDP because the native
    screenshot helper does not expose those arguments.
    """
    if uid and full_page:
        raise ValueError("Cannot combine uid and full_page.")
    if format not in {"png", "jpeg", "jpg", "webp"} or not 0 <= quality <= 100:
        raise ValueError("Invalid format or quality.")
    format = "jpeg" if format == "jpg" else format
    tab = await _tab_for_uid(uid) if uid else await _active_tab()
    if not uid and format in {"png", "jpeg"} and not quality:
        if file_path:
            saved = await tab.save_screenshot(
                filename=file_path, format=format, full_page=full_page
            )
            return f"Screenshot saved to {saved}."
        encoded = await tab.save_screenshot(
            format=format, full_page=full_page, as_base64=True
        )
    else:
        kwargs = {"format_": format, "capture_beyond_viewport": full_page}
        if quality and format in {"jpeg", "webp"}:
            kwargs["quality"] = quality
        if uid:
            model = await tab.send(
                uc.cdp.dom.get_box_model(
                    backend_node_id=uc.cdp.dom.BackendNodeId(
                        _uid_to_backend_node_id[uid]
                    )
                )
            )
            xs, ys = model.content[::2], model.content[1::2]
            kwargs["clip"] = uc.cdp.page.Viewport(
                min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys), 1
            )
        encoded = await tab.send(uc.cdp.page.capture_screenshot(**kwargs))
        if file_path:
            Path(file_path).write_bytes(base64.b64decode(encoded))
            return f"Screenshot saved to {file_path}."
    return Image(data=base64.b64decode(encoded), format=format)


@mcp.tool()
async def take_snapshot(
    verbose: bool = False, file_path: str = "", include_frames: bool = False
) -> str:
    """Take a text snapshot of the currently selected page based on the a11y tree.
    The snapshot lists page elements along with a unique identifier (uid).
    Always use the latest snapshot. Prefer taking a snapshot over taking a
    screenshot. The snapshot indicates searchable, structured text that is
    much smaller than an image or raw HTML.

    Args:
        verbose: Whether to include all possible information available in the full a11y tree. Default is false.
        file_path: Optional path to save the snapshot to instead of attaching it to the response.
    """
    global _snapshot_id
    tab = await _active_tab()
    page = await _page_tab()
    page_id = str(page.target_id)
    import nodriver.cdp.accessibility as cdp_a11y

    selected_frame = _selected_frames.get(page_id)
    frames = (
        await _discover_frames(page) if include_frames and not selected_frame else []
    )
    if not frames:
        frames = [(None, tab)]
    nodes = []
    node_connections = {}
    for frame, connection in frames:
        frame_id = str(frame.id_) if frame else selected_frame
        fetched = await connection.send(
            cdp_a11y.get_full_ax_tree(
                frame_id=uc.cdp.page.FrameId(frame_id) if frame_id else None
            )
        )
        prefix = f"{connection.target_id}:{frame_id or 'main'}:"
        for node in fetched:
            node_id = prefix + str(node.node_id)
            copied = dataclasses.replace(
                node,
                node_id=node_id,
                child_ids=[prefix + str(child) for child in node.child_ids]
                if node.child_ids
                else None,
                frame_id=node.frame_id
                or (uc.cdp.page.FrameId(frame_id) if frame_id else None),
            )
            nodes.append(copied)
            node_connections[node_id] = connection

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

    # --- Stable uid assignment (mirrors chrome-devtools-mcp) ---
    _snapshot_id += 1
    id_counter = 0
    uid_map: dict[str, str] = {}
    seen_unique_ids: set[str] = set()
    new_uid_to_backend: dict[str, int] = {}

    for node in nodes:
        frame_id = str(node.frame_id) if node.frame_id else ""
        backend_id = str(node.backend_dom_node_id) if node.backend_dom_node_id else ""
        unique_id = f"{page_id}:{frame_id}_{backend_id}"

        if backend_id and unique_id in _unique_id_to_mcp_id:
            uid_map[node.node_id] = _unique_id_to_mcp_id[unique_id]
        else:
            new_uid = f"{_snapshot_id}_{id_counter}"
            id_counter += 1
            uid_map[node.node_id] = new_uid
            if backend_id:
                _unique_id_to_mcp_id[unique_id] = new_uid

        if backend_id:
            seen_unique_ids.add(unique_id)

        # Record uid -> backend_node_id mapping for element resolution
        if node.backend_dom_node_id:
            assigned_uid = uid_map[node.node_id]
            new_uid_to_backend[assigned_uid] = int(node.backend_dom_node_id)
            _uid_owners[assigned_uid] = (
                page_id,
                node_connections[node.node_id],
                frame_id,
            )

    # Clean up stale mappings
    stale_keys = [
        k
        for k in _unique_id_to_mcp_id
        if k.startswith(page_id + ":") and k not in seen_unique_ids
    ]
    for k in stale_keys:
        del _unique_id_to_mcp_id[k]

    # Update global uid -> backend_node_id mapping
    for uid, owner in list(_uid_owners.items()):
        if owner[0] == page_id and uid not in new_uid_to_backend:
            _uid_owners.pop(uid, None)
            _uid_to_backend_node_id.pop(uid, None)
    _uid_to_backend_node_id.update(new_uid_to_backend)

    def _format_node(node_id: str, depth: int) -> str:
        node = node_map.get(node_id)
        if node is None:
            return ""

        role = ""
        if node.role and node.role.value:
            role = str(node.role.value)

        # Skip ignored nodes in non-verbose mode (promote children)
        if not verbose and node.ignored:
            child_parts = []
            for cid in children_map.get(node_id, []):
                child_parts.append(_format_node(cid, depth))
            return "".join(child_parts)

        # Skip roles entirely (node + descendants) — Chrome internals
        if not verbose and role in _SKIP_ROLES:
            return ""

        # Collapse container roles (skip node, promote children at same depth)
        if not verbose and role in _COLLAPSE_ROLES:
            name = ""
            if node.name and node.name.value:
                name = str(node.name.value)
            if not name:
                child_parts = []
                for cid in children_map.get(node_id, []):
                    child_parts.append(_format_node(cid, depth))
                return "".join(child_parts)

        name = ""
        if node.name and node.name.value:
            name = str(node.name.value)

        value = ""
        if node.value and node.value.value:
            value = str(node.value.value)

        # option special handling (same as chrome-devtools-mcp)
        if role == "option" and name and not value:
            value = name

        # --- Collect properties (matching Puppeteer's exposed set) ---
        props: list[str] = []
        if node.properties:
            for prop in node.properties:
                pname = (
                    prop.name.value if hasattr(prop.name, "value") else str(prop.name)
                )
                pval = (
                    prop.value.value
                    if prop.value and prop.value.value is not None
                    else None
                )

                if pname in _EXCLUDED_PROPERTIES or pname in _SUPPRESS_PROPERTIES:
                    continue

                if pval is False or pval == "false":
                    continue

                mapped = _BOOL_PROPERTY_MAP.get(pname)
                if mapped and (pval is True or pval == "true"):
                    props.append(mapped)

                if pval is True or pval == "true":
                    props.append(pname)
                elif isinstance(pval, (str, int, float)) and pval != "":
                    props.append(f'{pname}="{pval}"')

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

    if file_path:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(snapshot_text)
        return f"Snapshot saved to {file_path} ({len(snapshot_text)} chars)."

    return snapshot_text


@mcp.tool()
async def type_text(text: str, submit_key: str = "") -> str:
    """Insert Unicode text into the focused control using official CDP."""
    tab = await _active_tab()
    await tab.send(uc.cdp.input_.insert_text(text))
    if submit_key:
        await press_key(submit_key)
    return f"Typed {len(text)} characters" + (
        f", then pressed {submit_key}" if submit_key else ""
    )


@mcp.tool()
async def upload_file(uid: str, file_path: str, include_snapshot: bool = False) -> str:
    """Upload through the official Element.send_file helper."""
    await (await _element_for_uid(uid)).send_file(str(Path(file_path).resolve()))
    return f"Uploaded {file_path} to uid={uid}" + await _maybe_snapshot(
        include_snapshot
    )


@mcp.tool()
async def wait_for(text: list[str], timeout: int = 30000) -> str:
    """Wait for the specified text to appear on the selected page.

    Args:
        text: Non-empty list of texts. Resolves when any value appears on the page.
        timeout: Maximum wait time in milliseconds. Default is 30000.
    """
    tab = await _active_tab()
    timeout_s = timeout / 1000

    start = time.time()
    while time.time() - start < timeout_s:
        try:
            # Get page text content
            page_text = await _evaluate(
                tab, "document.body ? document.body.innerText : ''"
            )
            if page_text:
                for t in text:
                    if t in page_text:
                        # Found — return with snapshot
                        snapshot = await take_snapshot()
                        return f'Found text "{t}" on page.\n\n{snapshot}'
        except Exception:
            pass
        await asyncio.sleep(0.5)

    return f"Timeout: None of the texts {text} appeared within {timeout}ms."


# ---------------------------------------------------------------------------
# Session management helpers
# ---------------------------------------------------------------------------

_SESSIONS_DIR = os.path.join(os.path.expanduser("~"), ".nodriver-mcp", "sessions")


def _ensure_sessions_dir() -> str:
    os.makedirs(_SESSIONS_DIR, exist_ok=True)
    return _SESSIONS_DIR


@mcp.tool()
async def save_session(name: str) -> str:
    """Save all context cookies through CookieJar, plus this page's storage and URLs."""
    tab = await _page_tab()
    browser = await _get_browser()
    cookies = await (await _cookie_jar()).get_all()
    try:
        storage = await tab.get_local_storage()
    except uc.ProtocolException:
        storage = {}
    context_id = tab.target.browser_context_id
    session = {
        "version": 2,
        "name": name,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "current_url": tab.target.url,
        "pages": [
            t.target.url
            for t in browser.tabs
            if t.target.browser_context_id == context_id
        ],
        "cookies": [c.to_json() for c in cookies],
        "localStorage": storage,
    }
    safe_name = (
        "".join(c if c.isalnum() or c in "-_" else "_" for c in name) or "session"
    )
    filename = safe_name + "_" + time.strftime("%Y%m%d_%H%M%S") + ".json"
    path = Path(_ensure_sessions_dir()) / filename
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"Session '{name}' saved to {path}\n  Cookies: {len(cookies)}\n  localStorage items: {len(storage)}"


@mcp.tool()
async def load_session(filename: str, restore_pages: bool = False) -> str:
    """Restore JSON sessions using typed native CookieJar.set_all, including legacy files."""
    path = Path(filename) if os.path.isabs(filename) else Path(_SESSIONS_DIR) / filename
    session = json.loads(path.read_text(encoding="utf-8"))
    cookies = [_cookie_param(c) for c in session.get("cookies", [])]
    await (await _cookie_jar()).set_all(cookies)
    tab = await _page_tab()
    storage = session.get("localStorage", {})
    url = session.get("current_url", "")
    if url and url != "about:blank":
        _preserve_on_navigation(tab)
        await _auto_enable_network_collection(tab)
        await _navigate_url(tab, url)
        await tab
    if storage:
        await tab.set_local_storage(storage)
    opened = 0
    if restore_pages:
        for saved_url in session.get("pages", []):
            if saved_url and saved_url not in {"about:blank", url}:
                browser = await _get_browser()
                restored = await _open_new_tab(
                    browser,
                    url="about:blank",
                    background=True,
                    isolated_context="",
                    timeout=10000,
                )
                # Preserve the selected context when restoring additional pages.
                if tab.target.browser_context_id != restored.target.browser_context_id:
                    await restored.close()
                    target = await browser.send(
                        uc.cdp.target.create_target(
                            "about:blank",
                            browser_context_id=tab.target.browser_context_id,
                            background=True,
                        )
                    )
                    restored = await _wait_for_target(browser, target, 10000)
                await _auto_enable_network_collection(restored)
                await _navigate_url(restored, saved_url)
                opened += 1
    if url and url != "about:blank":
        await tab.reload()
        await tab
    return (
        f"Session '{session.get('name', '')}' loaded from {path}\n"
        f"  Cookies restored: {len(cookies)}\n  localStorage items restored: {len(storage)}\n"
        f"  Pages re-opened: {opened}"
    )


@mcp.tool()
async def list_sessions() -> str:
    """List all saved browser sessions.

    Returns the available session files with their names and save times.
    """
    _ensure_sessions_dir()
    files = sorted(
        [f for f in os.listdir(_SESSIONS_DIR) if f.endswith(".json")],
        reverse=True,
    )

    if not files:
        return "No saved sessions found."

    lines = [f"Saved sessions ({len(files)}):"]
    for f in files:
        try:
            filepath = os.path.join(_SESSIONS_DIR, f)
            with open(filepath, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            name = data.get("name", "unknown")
            saved_at = data.get("saved_at", "unknown")
            n_cookies = len(data.get("cookies", []))
            n_ls = len(data.get("localStorage", {}))
            lines.append(f"  {f}")
            lines.append(
                f"    Name: {name} | Saved: {saved_at} | Cookies: {n_cookies} | localStorage: {n_ls}"
            )
        except Exception:
            lines.append(f"  {f} (unable to read)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Run the MCP server via stdio transport."""
    mcp.run(transport="stdio")


from .advanced import register_tools

register_tools(mcp, sys.modules[__name__])


if __name__ == "__main__":
    main()
