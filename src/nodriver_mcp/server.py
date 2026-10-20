"""
nodriver-mcp: An undetected Chrome automation MCP server.

Uses nodriver (successor of undetected-chromedriver) as the browser backend,
providing the same MCP tool interface as chrome-devtools-mcp but without
exposing CDP/WebDriver fingerprints that get detected by anti-bot systems.
"""

import asyncio
import base64
import dataclasses
import inspect
import itertools
import json
import logging
import os
import sys
import time
import weakref
from contextlib import asynccontextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, Literal

import nodriver as uc
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, InputRequiredResult, Tool as MCPTool
from pydantic import StrictBool, ValidationError

from .actions import ActionResult, ActionWaiter
from .console import ConsoleCollector, format_message, format_stack
from .contexts import FrameContexts
from .dialogs import DialogInterrupted, DialogRegistry
from .emulation import EmulationOverrides, FrameOverrides, http_headers, media_updates
from .forms import control_state, fill_text, fill_toggle
from .launch import LaunchOptions, ProfileLease
from .keyboard import dispatch_shortcut, parse_shortcut
from .network import NetworkCollector, request_details
from .performance import analyze_trace_file
from .runtime_policy import RUNTIME_ENABLE_WARNING
from .screenshots import screenshot_clip
from .scripts import (
    execute_script,
    prepare_script_call,
    script_result,
)
from .source_maps import symbolize_message, symbolize_messages
from .tool_groups import (
    ENABLED_GROUPS_ENV,
    TOOL_GROUP_BY_NAME,
    parse_enabled_tool_groups,
)
from .traces import TRACE_CATEGORIES, save_trace
from .uploads import FileUploads, upload_paths
from .workers import WorkerTargets, worker_info

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("nodriver-mcp")


# ---------------------------------------------------------------------------
# Global browser state
# ---------------------------------------------------------------------------
_browser: uc.Browser | None = None
_browser_lock = asyncio.Lock()
_launch_options: LaunchOptions | None = None
_profile_lease: ProfileLease | None = None
_default_browser_context_id = None
_file_uploads = FileUploads()
_dialog_state = DialogRegistry()


async def _get_browser() -> uc.Browser:
    """Lazily launch a temporary browser, or continue the explicitly chosen session."""
    async with _browser_lock:
        return await _ensure_browser()


async def _ensure_browser(
    options: LaunchOptions | None = None, *, restart: bool = False
) -> uc.Browser:
    """Start/reconfigure the browser while the caller owns _browser_lock."""
    global _browser, _selected_target_id, _launch_options, _profile_lease
    global _default_browser_context_id
    options = options or _launch_options or LaunchOptions.resolve()
    if (
        _browser is not None
        and not _browser.stopped
        and options == _launch_options
        and not restart
    ):
        return _browser
    if _browser is not None:
        await _shutdown_browser()
    try:
        if options.user_data_dir:
            _profile_lease = ProfileLease(options.user_data_dir)
            _profile_lease.acquire()
        config = options.config()
        # Keep the official Browser instance during start so partial launch
        # failures can also stop their process and remove their temporary profile.
        _browser = uc.Browser(config)
        _launch_options = options
        for extension in options.extensions:
            config.add_extension(extension)
        await _browser.start()
        _default_browser_context_id = _browser.main_tab.target.browser_context_id
        _selected_target_id = str(_browser.main_tab.target_id)
        await _prepare_page(_browser.main_tab)
        await _auto_enable_network_collection(_browser.main_tab)
        logger.info(
            "Browser started (headless=%s, persistent=%s, profile=%s)",
            options.headless,
            options.persistent,
            options.profile if options.persistent else "temporary",
        )
    except BaseException:
        await _shutdown_browser()
        raise
    return _browser


def _browser_launch_info() -> dict:
    if _browser is None or _launch_options is None:
        return {"running": False}
    return {
        "running": not _browser.stopped,
        "persistent": _launch_options.persistent,
        "profile": _launch_options.profile if _launch_options.persistent else None,
        "user_data_dir": _browser.config.user_data_dir,
        "proxy": _launch_options.proxy,
        "headless": _launch_options.headless,
    }


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
class BrowserMCPServer(MCPServer):
    """Expose enabled tools and report expected browser failures through ToolError."""

    def __init__(self, *args, **kwargs):
        self.enabled_tool_groups = parse_enabled_tool_groups(
            os.environ.get(ENABLED_GROUPS_ENV, "")
        )
        super().__init__(*args, **kwargs)

    def _disabled_tool_group(self, name: str) -> str | None:
        group = TOOL_GROUP_BY_NAME.get(name)
        return group if group and group not in self.enabled_tool_groups else None

    async def list_tools(self) -> list[MCPTool]:
        return [
            tool
            for tool in await super().list_tools()
            if self._disabled_tool_group(tool.name) is None
        ]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context | None = None,
    ) -> CallToolResult | InputRequiredResult:
        group = self._disabled_tool_group(name)
        if group is not None:
            raise ToolError(
                f"Tool '{name}' is disabled. Add '{group}' to "
                f"{ENABLED_GROUPS_ENV} and restart the MCP server to enable it."
            )
        try:
            return await super().call_tool(name, arguments, context)
        except UnexpectedToolError as exc:
            cause = exc.__cause__
            if isinstance(
                cause, (ValueError, TimeoutError, uc.ProtocolException)
            ) and not isinstance(cause, ValidationError):
                raise ToolError(str(cause)) from cause
            raise


@asynccontextmanager
async def _lifespan(_server):
    try:
        yield
    finally:
        await _shutdown_browser()


mcp = BrowserMCPServer(
    "nodriver-mcp",
    version=package_version("nodriver-mcp"),
    lifespan=_lifespan,
    instructions=(
        "Undetected Chrome browser automation via nodriver. "
        "Drop-in replacement for chrome-devtools-mcp that avoids CDP fingerprint detection. "
        + RUNTIME_ENABLE_WARNING
        + " Ordinary browsing, scripts, HTTP inspection and Network WebSocket events "
        "do not need Runtime.enable. Console tools are optional; CDP/event tools "
        "require allow_runtime_enable=true to enable Runtime. "
        "IMPORTANT: Always use take_snapshot instead of take_screenshot to read page content. "
        "take_snapshot returns searchable HTML text and is much faster and smaller. "
        "Only use take_screenshot when you specifically need a visual image for layout checks or visual regression. "
        "When the user requests a proxy or persistent session, call start_browser "
        "with the requested proxy/persistent/profile and URL BEFORE opening any website. "
        "Otherwise the browser starts with a fresh temporary profile by default. "
        "Use persistent=true only when requested; reuse profile='default' when no name is given. "
        "Subsequent tools continue the chosen session. Changing startup options restarts Chrome "
        "and closes its current pages. close_browser ends the session. "
        "Page tools accept optional page_id: use the stable string ID from list_pages "
        "or new_page to operate on multiple pages concurrently without select_page. "
        "Use list_pages(include_service_workers=true) to discover running service_worker_id "
        "targets for evaluate_script and console tools; worker IDs exclude page_id and element args. "
        "An explicit page_id does not change the default selected page. Numeric indexes "
        "are also accepted for compatibility but can change when pages close. "
        "NOTE: The browser is also launched lazily on the first ordinary browser tool call. "
        "The first invocation may take a few extra seconds for Chrome to start — this is normal, just wait for it."
    ),
)


@dataclasses.dataclass
class _PageCall:
    requested: int | str | None
    page: uc.Tab | None = None
    frame_id: str | None = None
    waiter: ActionWaiter | None = None
    action_results: list[ActionResult] = dataclasses.field(default_factory=list)
    blocked_by_dialog: bool = False


_page_call: ContextVar[_PageCall | None] = ContextVar("page_call", default=None)


def _page_tool(*, prepare=None, **options):
    """Publish an explicit page_id parameter and bind nested work to one page."""

    def register(fn):
        signature = inspect.signature(fn)

        @wraps(fn)
        async def routed(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            arguments = bound.arguments
            if (
                arguments.get("service_worker_id")
                and arguments.get("page_id") is not None
            ):
                raise ValueError(
                    "Specify either page_id or service_worker_id, not both."
                )
            if prepare is not None:
                await prepare(arguments)
                args, kwargs = bound.args, bound.kwargs
            requested = arguments.get("page_id")
            if fn.__name__ == "close_page" and requested == -1:
                requested = None  # Legacy spelling for the selected page.
            if _page_call.get() is not None and requested is None:
                return await fn(*args, **kwargs)
            call = _PageCall(
                requested,
                blocked_by_dialog=fn.__name__
                in {
                    "click",
                    "click_at",
                    "fill",
                    "fill_form",
                    "press_key",
                    "type_text",
                    "drag",
                    "hover",
                    "upload_file",
                    "navigate_page",
                    "take_snapshot",
                    "take_screenshot",
                    "find_elements",
                    "list_frames",
                    "select_frame",
                    "get_local_storage",
                    "set_local_storage",
                    "take_memory_snapshot",
                    "emulate",
                    "emulate_device",
                    "resize_page",
                    "scroll_page",
                    "wait_for",
                },
            )
            token = _page_call.set(call)
            try:
                if requested is not None:
                    if arguments.get("scope") == "browser":
                        raise ValueError("page_id applies only to scope='page'.")
                    # Validate explicit IDs even for tools that use retained state.
                    await _page_tab()
                result = await fn(*args, **kwargs)
                summaries = [r.summary() for r in call.action_results if r.summary()]
                if isinstance(result, str) and summaries:
                    result += "\n\n" + "\n".join(summaries)
                return result
            finally:
                _page_call.reset(token)

        routing_help = (
            "\n\npage_id: Optional stable string ID from list_pages/new_page, or a "
            "current numeric list index. Omit to use the selected page. Explicit "
            "IDs leave the default selection unchanged; prefer stable IDs for concurrency."
        )
        routed.__doc__ = (fn.__doc__ or "") + routing_help
        settings = dict(options)
        if settings.get("description"):
            settings["description"] += routing_help
        return mcp.tool(**settings)(routed)

    return register


async def _page_tab() -> uc.Tab:
    """Resolve once per tool call; explicit IDs never fall back to another page."""
    global _selected_target_id
    call = _page_call.get()
    if call is not None and call.page is not None:
        record = _dialog_state.current.get(str(call.page.target_id))
        if record is not None and call.blocked_by_dialog:
            raise DialogInterrupted(record)
        return call.page
    browser = await _get_browser()
    await browser.update_targets()
    requested = call.requested if call is not None else None
    if requested is not None:
        if isinstance(requested, bool) or not isinstance(requested, (int, str)):
            raise ValueError(
                "page_id must be a stable string ID or numeric list index."
            )
        if isinstance(requested, int):
            tab = (
                browser.tabs[requested] if 0 <= requested < len(browser.tabs) else None
            )
        else:
            tab = next((t for t in browser.tabs if str(t.target_id) == requested), None)
        if tab is None:
            raise ValueError(
                f"Unknown or closed page_id {requested!r}. Use list_pages."
            )
    else:
        tab = next(
            (t for t in browser.tabs if str(t.target_id) == _selected_target_id), None
        )
        if tab is None:
            if not browser.tabs:
                raise ValueError("No open pages. Use new_page first.")
            tab = browser.tabs[-1]
            _selected_target_id = str(tab.target_id)
    await _prepare_page(tab)
    await _emulation_overrides.sync(tab, str(tab.target_id))
    record = _dialog_state.current.get(str(tab.target_id))
    if record is not None and call is not None and call.blocked_by_dialog:
        raise DialogInterrupted(record)
    if call is not None:
        call.page = tab
        call.frame_id = _selected_frames.get(str(tab.target_id))
    return tab


def _selected_frame_id(page: uc.Tab) -> str | None:
    call = _page_call.get()
    if call is not None and call.page is page:
        return call.frame_id
    return _selected_frames.get(str(page.target_id))


def _select_frame(page: uc.Tab, frame_id: str | None) -> None:
    key = str(page.target_id)
    if frame_id:
        _selected_frames[key] = frame_id
    else:
        _selected_frames.pop(key, None)
    call = _page_call.get()
    if call is not None and call.page is page:
        call.frame_id = frame_id


async def _active_tab() -> uc.Tab:
    page = await _page_tab()
    page_id = str(page.target_id)
    frame_id = _selected_frame_id(page)
    if not frame_id:
        return page
    if page_id in _dialog_state.current:
        cached = _frame_connections.get((page_id, frame_id))
        if cached is not None:
            return cached
    await _discover_frames(page)
    connection = _frame_connections.get((page_id, frame_id))
    if connection is None:
        raise ValueError(
            "Selected frame no longer exists; use select_frame or list_frames."
        )
    return connection


# ---------------------------------------------------------------------------
# Shared state for console / network collection
# ---------------------------------------------------------------------------
_console_messages: list[dict] = []
_network_requests: list[dict] = []
_preserved_console_messages: list[list[dict]] = []  # last 3 navigations
_preserved_network_requests: list[list[dict]] = []  # last 3 navigations
_tracing_active = False
_trace_stopping = False
_network_collection_enabled_tabs: set[int] = set()
_console_collection_enabled_tabs: set[int] = set()
_named_browser_contexts: dict[
    str, Any
] = {}  # isolated_context name -> BrowserContextID
_selected_target_id: str | None = None
_selected_frames: dict[str, str] = {}
_closing_page_ids: set[str] = set()
_frame_connections: dict[tuple[str, str], uc.Tab] = {}
_frame_loaders: dict[tuple[str, str], str] = {}
_frame_discovery_locks: dict[str, asyncio.Lock] = {}
_console_callbacks: dict[int, ConsoleCollector] = {}
_network_callbacks: dict[int, NetworkCollector] = {}
_collection_locks: dict[int, tuple[Any, asyncio.Lock]] = {}
_uid_owners: dict[str, tuple[str, uc.Tab, str]] = {}
_request_ids = itertools.count()
_message_ids = itertools.count()
_trace_tab: uc.Tab | None = None
_trace_metadata: dict = {}
_last_trace_path = ""
_shutdown_callbacks: list[Any] = []
_page_cleanup_callbacks: list[Any] = []
_focused_pages: dict[int, Any] = {}
_worker_targets = WorkerTargets(lambda tab: _clear_collection_state(tab))
_emulation_overrides = EmulationOverrides(
    lambda tab: _auto_enable_network_collection(tab)
)
_frame_overrides = FrameOverrides(_emulation_overrides)
_frame_contexts = FrameContexts()


async def _prepare_page(tab: uc.Tab) -> None:
    key = id(tab)
    reference = _focused_pages.get(key)
    if reference is not None and reference() is tab:
        return
    await tab.send(uc.cdp.emulation.set_focus_emulation_enabled(True))
    _frame_contexts.watch(tab)
    _dialog_state.watch(tab, str(tab.target_id))

    def retired(reference):
        if _focused_pages.get(key) is reference:
            _focused_pages.pop(key, None)

    _focused_pages[key] = weakref.ref(tab, retired)


def _page_key(tab: uc.Tab) -> str:
    current = tab
    while getattr(current, "parent", None) is not None:
        parent = current.parent
        if isinstance(parent, uc.Browser):
            break
        current = parent
    return str(current.target_id)


def _tab_key(tab: uc.Tab) -> int:
    """Scope collectors to a connection instance; Chrome can reuse target IDs."""
    return id(tab)


def _clear_collection_state(tab: uc.Tab) -> None:
    """Remove this connection's collectors before retiring its transport."""
    key = _tab_key(tab)
    _collection_locks.pop(key, None)
    _frame_contexts.forget(tab)
    _focused_pages.pop(key, None)
    _emulation_overrides.retire(tab)
    _dialog_state.forget(tab)
    collector = _console_callbacks.pop(key, None)
    if collector is not None:
        collector.remove()
    _console_collection_enabled_tabs.discard(key)
    network = _network_callbacks.pop(key, None)
    if network is not None:
        network.remove()
    _network_collection_enabled_tabs.discard(key)
    for record in [
        r for batch in _preserved_network_requests for r in batch
    ] + _network_requests:
        if record["connection"] is tab:
            record["connection_active"] = False


async def _forget_page_state(tab: uc.Tab) -> None:
    """Retire only this page's managed state and its iframe transports."""
    global _selected_target_id
    key = str(tab.target_id)
    for cleanup in _page_cleanup_callbacks:
        await cleanup(key)
    await _frame_overrides.close(key)
    _clear_collection_state(tab)
    _emulation_overrides.forget(key)
    _selected_frames.pop(key, None)
    _frame_discovery_locks.pop(key, None)
    for pair, connection in list(_frame_connections.items()):
        if pair[0] == key:
            if connection is not tab:
                _clear_collection_state(connection)
                await connection.aclose()
            del _frame_connections[pair]
            _frame_loaders.pop(pair, None)
    for uid, owner in list(_uid_owners.items()):
        if owner[0] == key:
            _uid_owners.pop(uid, None)
            _uid_to_backend_node_id.pop(uid, None)
    for unique_id in list(_unique_id_to_mcp_id):
        if unique_id.startswith(key + ":"):
            del _unique_id_to_mcp_id[unique_id]
    for records in (_console_messages, _network_requests):
        records[:] = [r for r in records if r["page_id"] != key]
    for history in (_preserved_console_messages, _preserved_network_requests):
        history[:] = [batch for batch in history if batch[0]["page_id"] != key]
    if _selected_target_id == key:
        _selected_target_id = None


async def _shutdown_browser() -> None:
    global _browser, _selected_target_id, _tracing_active, _trace_tab, _trace_stopping
    global _launch_options, _profile_lease, _trace_metadata
    global _default_browser_context_id
    for callback in _shutdown_callbacks:
        try:
            await callback()
        except Exception:
            logger.debug("Tool cleanup failed", exc_info=True)
    await _frame_overrides.close_all()
    browser, _browser = _browser, None
    lease, _profile_lease = _profile_lease, None
    _launch_options = None
    _default_browser_context_id = None
    connections = list(_frame_connections.values()) + list(
        _worker_targets.connections.values()
    )
    try:
        if browser is not None:
            process = browser._process
            if process is not None and process.returncode is None:
                try:
                    # Flush Chrome's persistent cookies/storage before closing
                    # transport; native stop() terminates the process directly.
                    await asyncio.wait_for(browser.send(uc.cdp.browser.close()), 3)
                except Exception:
                    logger.debug("Graceful browser close did not reply", exc_info=True)
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    browser.stop()
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
            connections.extend(browser.targets)
            for connection in {id(c): c for c in connections}.values():
                try:
                    _clear_collection_state(connection)
                    await connection.aclose()
                except Exception:
                    logger.debug("Connection cleanup failed", exc_info=True)
            try:
                await browser.aclose()
            except Exception:
                logger.debug("Browser connection cleanup failed", exc_info=True)
            # Official cleanup removes only native temporary profiles. The
            # persistent profile remains available for the next MCP process.
            await uc.util.deconstruct_browser(browser)
            uc.util.get_registered_instances().discard(browser)
    finally:
        if lease is not None:
            lease.release()
    for state in (
        _network_collection_enabled_tabs,
        _console_collection_enabled_tabs,
        _named_browser_contexts,
        _selected_frames,
        _closing_page_ids,
        _frame_connections,
        _frame_loaders,
        _frame_discovery_locks,
        _console_callbacks,
        _network_callbacks,
        _collection_locks,
        _focused_pages,
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
    _dialog_state.clear()
    _frame_contexts.clear()
    _emulation_overrides.clear()
    _worker_targets.clear()
    _tracing_active = False
    _trace_stopping = False
    _trace_tab = None
    _trace_metadata = {}


async def _evaluate(tab: uc.Tab, expression: str, await_promise: bool = True) -> Any:
    return await execute_script(
        tab,
        expression,
        None,
        format="script",
        await_promise=await_promise,
        resolve_context=lambda group: _frame_execution_context(tab, object_group=group),
    )


async def _frame_execution_context(tab, *, object_group):
    root_id = _page_key(tab)
    call = _page_call.get()
    frame_id = (
        call.frame_id
        if call is not None
        and call.page is not None
        and str(call.page.target_id) == root_id
        else _selected_frames.get(root_id)
    )
    if (
        not frame_id
        or frame_id == str(tab.target_id)
        or _frame_connections.get((root_id, frame_id)) is not tab
    ):
        return None
    lock = _collection_lock(tab)
    async with lock:
        _check_collection_owner(tab, lock)
        context = await _frame_contexts.main_world(
            tab, frame_id, object_group=object_group
        )
        _check_collection_owner(tab, lock)
        return context


async def _tab_for_uid(uid: str, *, refresh: bool = True) -> uc.Tab:
    owner = _uid_owners.get(uid)
    page = await _page_tab()
    if refresh and owner is not None and owner[0] == str(page.target_id) and owner[2]:
        await _discover_frames(page)
        owner = _uid_owners.get(uid)
    if owner is None or owner[0] != str(page.target_id):
        raise ValueError(
            f"Unknown or stale uid '{uid}' for this page. Take a new snapshot."
        )
    return owner[1]


async def _element_for_uid(uid: str, *, refresh: bool = True) -> uc.Element:
    tab = await _tab_for_uid(uid, refresh=refresh)
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
    async with _frame_discovery_locks.setdefault(root_id, asyncio.Lock()):
        tree = await page.get_frame_tree()
        discovered = {
            str(frame.id_): (frame, page) for frame in uc.util.flatten_frame_tree(tree)
        }
        owners = {str(tree.frame.id_): page}
        for native in await page.get_frames():
            frame_id = str(native.target_id)
            cached = _frame_connections.get((root_id, frame_id))
            # get_frames creates new wrappers on every call. Reuse a connection
            # only while the frame still has its own current target; a former
            # same-process frame must switch from the page to the native iframe.
            connection = (
                cached
                if cached is not None
                and cached is not page
                and cached.target
                and str(cached.target_id) == frame_id
                else native
            )
            connection.target = native.target
            try:
                subtree = await connection.get_frame_tree()
            except Exception:
                if connection is native:
                    _clear_collection_state(connection)
                    await connection.aclose()
                raise
            frame = subtree.frame
            if frame.parent_id is None:
                frame = dataclasses.replace(
                    frame, parent_id=native.target.parent_frame_id
                )
            _frame_contexts.watch(connection)
            subtree = dataclasses.replace(subtree, frame=frame)
            for child in uc.util.flatten_frame_tree(subtree):
                discovered[str(child.id_)] = (child, page)
            owners[str(frame.id_)] = connection

        def frame_connection(frame_id):
            if frame_id in owners:
                return owners[frame_id]
            frame = discovered[frame_id][0]
            parent_id = str(frame.parent_id) if frame.parent_id else None
            connection = (
                frame_connection(parent_id) if parent_id in discovered else page
            )
            owners[frame_id] = connection
            return connection

        discovered = {
            frame_id: (frame, frame_connection(frame_id))
            for frame_id, (frame, _) in discovered.items()
        }

        retired = {}
        for frame_id, (frame, connection) in discovered.items():
            _dialog_state.watch(connection, root_id)
            await _emulation_overrides.sync(connection, root_id)
            key = (root_id, frame_id)
            previous = _frame_connections.get(key)
            loader = str(frame.loader_id)
            if previous is not None and (
                previous is not connection or _frame_loaders.get(key) != loader
            ):
                _invalidate_frame_uids(root_id, frame_id)
                if previous is not page:
                    retired[id(previous)] = previous
            _frame_connections[key] = connection
            _frame_loaders[key] = loader

        for key in list(_frame_connections):
            if key[0] == root_id and key[1] not in discovered:
                _invalidate_frame_uids(*key)
                previous = _frame_connections.pop(key)
                _frame_loaders.pop(key, None)
                if previous is not page:
                    retired[id(previous)] = previous
        live = {id(connection) for connection in _frame_connections.values()}
        for connection_id, connection in retired.items():
            if connection_id not in live:
                _clear_collection_state(connection)
                await connection.aclose()
        return list(discovered.values())


def _invalidate_frame_uids(page_id: str, frame_id: str) -> None:
    for uid, owner in list(_uid_owners.items()):
        if owner[0] == page_id and owner[2] == frame_id:
            _uid_owners.pop(uid, None)
            _uid_to_backend_node_id.pop(uid, None)
    prefix = f"{page_id}:{frame_id}_"
    for key in list(_unique_id_to_mcp_id):
        if key.startswith(prefix):
            del _unique_id_to_mcp_id[key]


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


def _preserve_on_navigation(tab: uc.Tab) -> None:
    page_id = _page_key(tab)
    for current, history in [
        (_console_messages, _preserved_console_messages),
        (_network_requests, _preserved_network_requests),
    ]:
        batch = [item for item in current if item["page_id"] == page_id]
        if batch:
            history.append(batch)
            page_batches = [h for h in history if h[0]["page_id"] == page_id]
            expired = {id(h) for h in page_batches[:-3]}
            history[:] = [h for h in history if id(h) not in expired]
            current[:] = [item for item in current if item["page_id"] != page_id]
    for uid, owner in list(_uid_owners.items()):
        if owner[0] == page_id:
            _uid_owners.pop(uid, None)
            _uid_to_backend_node_id.pop(uid, None)
    _select_frame(tab, None)
    for unique_id in list(_unique_id_to_mcp_id):
        if unique_id.startswith(page_id + ":"):
            del _unique_id_to_mcp_id[unique_id]
    for key, connection in list(_frame_connections.items()):
        if key[0] == page_id:
            if connection is not tab:
                _clear_collection_state(connection)
                asyncio.create_task(connection.aclose())
            del _frame_connections[key]
            _frame_loaders.pop(key, None)


# ---------------------------------------------------------------------------
# Auto-collection for network (console collection is explicit)
# ---------------------------------------------------------------------------
def _collection_lock(tab):
    key = _tab_key(tab)
    entry = _collection_locks.get(key)
    if entry is not None and entry[0]() is tab:
        return entry[1]

    def retired(reference):
        current = _collection_locks.get(key)
        if current is not None and current[0] is reference:
            _collection_locks.pop(key, None)

    lock = asyncio.Lock()
    _collection_locks[key] = (weakref.ref(tab, retired), lock)
    return lock


def _owns_collection(tab, lock):
    entry = _collection_locks.get(_tab_key(tab))
    return entry is not None and entry[0]() is tab and entry[1] is lock


def _check_collection_owner(tab, lock):
    if not _owns_collection(tab, lock):
        raise ValueError("Connection was retired during collection initialization.")


async def _auto_enable_network_collection(tab: uc.Tab) -> None:
    """Collect typed network events on their originating connection."""
    lock = _collection_lock(tab)
    async with lock:
        _check_collection_owner(tab, lock)
        await _start_network_collection(tab, lock)


async def _start_network_collection(tab, lock):
    key = _tab_key(tab)
    if key in _network_collection_enabled_tabs:
        return

    def collect(record):
        if not _owns_collection(tab, lock):
            return
        record.update(reqid=next(_request_ids), page_id=_page_key(tab), connection=tab)
        _network_requests.append(record)
        own = [r for r in _network_requests if r["page_id"] == record["page_id"]]
        expired = {r["reqid"] for r in own[:-1000]}
        if expired:
            _network_requests[:] = [
                r for r in _network_requests if r["reqid"] not in expired
            ]

    collector = NetworkCollector(tab, collect)
    try:
        await collector.enable()
        _check_collection_owner(tab, lock)
    except BaseException:
        collector.remove()
        raise
    _network_callbacks[key] = collector
    _network_collection_enabled_tabs.add(key)


async def _enable_console_collection(
    tab: uc.Tab, source_maps: bool | None = None
) -> bool:
    lock = _collection_lock(tab)
    async with lock:
        _check_collection_owner(tab, lock)
        return await _start_console_collection(tab, source_maps, lock)


async def _start_console_collection(tab, source_maps, lock):
    key = _tab_key(tab)
    if key in _console_collection_enabled_tabs:
        collector = _console_callbacks[key]
        if source_maps is True:
            await collector.source_maps.enable()
        elif source_maps is False:
            await collector.source_maps.disable()
        _check_collection_owner(tab, lock)
        return False

    def collect(record):
        if not _owns_collection(tab, lock):
            return
        record.update(msgid=next(_message_ids), page_id=_page_key(tab), connection=tab)
        if getattr(tab.target, "type_", None) == "service_worker":
            record.update({k: v for k, v in worker_info(tab).items() if k != "url"})
        _console_messages.append(record)
        del _console_messages[:-1000]

    def records():
        return [
            m for batch in _preserved_console_messages for m in batch
        ] + _console_messages

    collector = ConsoleCollector(tab, collect, records)
    try:
        await collector.enable(source_maps=bool(source_maps))
        _check_collection_owner(tab, lock)
    except BaseException:
        collector.remove()
        raise
    _console_callbacks[key] = collector
    _console_collection_enabled_tabs.add(key)
    return True


async def _disable_console_collection(tab: uc.Tab) -> bool:
    lock = _collection_lock(tab)
    async with lock:
        _check_collection_owner(tab, lock)
        return await _stop_console_collection(tab)


async def _stop_console_collection(tab):
    key = _tab_key(tab)
    collector = _console_callbacks.pop(key, None)
    if collector is None:
        return False
    _console_collection_enabled_tabs.discard(key)
    try:
        await collector.source_maps.disable()
    finally:
        collector.remove()
    if not _frame_contexts.has_runtime_consumers(tab):
        try:
            await tab.send(uc.cdp.runtime.disable())
        except BaseException:
            await _frame_contexts.restore_runtime(tab)
            raise
        _frame_contexts.reset(tab)
    return True


# ---------------------------------------------------------------------------
# UID resolution: uid -> DOM element operations
# ---------------------------------------------------------------------------
async def _resolve_uid(
    tab: uc.Tab, uid: str, *, object_group: str | None = None
) -> Any:
    connection = await _tab_for_uid(uid)
    result = await connection.send(
        uc.cdp.dom.resolve_node(
            backend_node_id=uc.cdp.dom.BackendNodeId(_uid_to_backend_node_id[uid]),
            object_group=object_group,
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


async def _dispatch_mouse_click(tab: uc.Tab, x: float, y: float, double: bool) -> None:
    for count in range(1, 3 if double else 2):
        for event in ("mousePressed", "mouseReleased"):
            await tab.send(
                uc.cdp.input_.dispatch_mouse_event(
                    event,
                    x=x,
                    y=y,
                    button=uc.cdp.input_.MouseButton.LEFT,
                    buttons=1 if event == "mousePressed" else 0,
                    click_count=count,
                )
            )


async def _perform_action(
    tab: uc.Tab,
    action,
    *,
    wait_for_stable: bool = True,
    preserve_navigation: bool = True,
    ignored_dialogs: frozenset[str] = frozenset(),
    dialog_action: str | None = None,
    prompt_text: str = "",
):
    call = _page_call.get()
    if call is not None and call.waiter is not None:
        return await action()
    browser = await _get_browser()
    page = call.page if call is not None else None
    page = page or next(
        (t for t in browser.tabs if str(t.target_id) == _page_key(tab)), tab
    )

    def navigated(frame_id):
        if not preserve_navigation:
            return
        if frame_id == waiter.root_frame_id:
            _preserve_on_navigation(page)
            if call is not None:
                call.frame_id = None
        else:
            _invalidate_frame_uids(str(page.target_id), frame_id)

    waiter = ActionWaiter(
        tab,
        page,
        browser,
        _evaluate,
        navigated,
        frame_id=_selected_frame_id(page),
        wait_for_stable=wait_for_stable,
        ignored_dialogs=ignored_dialogs,
        dialogs=_dialog_state,
        dialog_action=dialog_action,
        prompt_text=prompt_text,
    )
    if call is not None:
        call.waiter = waiter
    try:
        return await waiter.run(action)
    finally:
        if call is not None:
            call.waiter = None
            call.action_results.append(waiter.result)


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
    forced_colors: str = "",
    prefers_contrast: str = "",
    prefers_reduced_motion: str = "",
    extra_http_headers: dict[str, str] | str | None = None,
) -> list[str]:
    """Apply emulation settings and return a human-readable summary."""
    updates = media_updates(
        color_scheme=color_scheme,
        forced_colors=forced_colors,
        prefers_contrast=prefers_contrast,
        prefers_reduced_motion=prefers_reduced_motion,
    )
    headers = http_headers(extra_http_headers)
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

    if updates or headers is not None:
        page = await _page_tab()
        await _frame_overrides.watch(page)
        frames = await _discover_frames(page)
        page_id = str(page.target_id)
        await _emulation_overrides.update(
            page_id,
            [page, *(c for _, c in frames), *_frame_overrides.connections(page_id)],
            updates,
            headers,
        )
        results.extend(f"{name}={value or 'auto'}" for name, value in updates.items())
        if headers is not None:
            results.append(f"extra_http_headers={len(headers)}")

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

    page = await _page_tab()
    frames = await _discover_frames(page)
    await _emulation_overrides.update(
        str(page.target_id),
        [
            page,
            *(c for _, c in frames),
            *_frame_overrides.connections(str(page.target_id)),
        ],
        reset=True,
    )
    results.append("media_and_headers=reset")

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
    # Context names and their pages share the browser lifecycle lock with
    # explicit context creation/disposal and browser restarts.
    async with _browser_lock if isolated_context else nullcontext():
        ctx = (
            _named_browser_contexts.get(isolated_context) if isolated_context else None
        )
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
        f"{tab.target.url} — {tab.target.title} (page_id={tab.target_id})"
        for i, tab in enumerate(browser.tabs)
    )


# ---------------------------------------------------------------------------
# Tools (aligned with chrome-devtools-mcp interface)
# ---------------------------------------------------------------------------


@mcp.tool()
async def start_browser(
    url: str = "",
    proxy: str | None = None,
    persistent: bool = False,
    profile: str = "default",
    restart: bool = False,
    timeout: int = 30000,
) -> dict:
    """Start Chrome with optional proxy and persistent login state, then open a URL.

    Call this BEFORE opening the requested website when the user specifies a
    proxy or persistent session. Otherwise ordinary tools start a temporary
    session automatically. This configures the browser behind the running MCP
    server; the user does not need to edit the MCP client's configuration.

    Args:
        url: Website to open after applying the launch settings. Empty leaves the page unchanged.
        proxy: HTTP/HTTPS/SOCKS4/SOCKS5 server, e.g. http://127.0.0.1:7890. Null inherits NODRIVER_PROXY/system settings; an empty string forces direct access.
        persistent: False (default) uses a fresh temporary profile per launch; true reuses cookies/storage across future launches and MCP restarts.
        profile: Persistent profile name. Use default when the user does not name one; use the same name on future launches. Requires persistent=true for other names.
        restart: Force a new browser, including a fresh profile for temporary sessions. Changed launch options automatically restart Chrome and close its current pages.
        timeout: Navigation timeout in milliseconds; 0 disables the timeout.

    Matching settings reuse the running browser. Subsequent tools keep using
    the chosen proxy and session. Persistent profiles retain unexpired persistent
    cookies and site storage; websites may still expire or revoke a login.
    """
    global _selected_target_id
    if timeout < 0:
        raise ValueError("timeout must be nonnegative.")
    options = LaunchOptions.resolve(persistent=persistent, profile=profile, proxy=proxy)
    async with _browser_lock:
        running = _browser is not None and not _browser.stopped
        changed = restart or options != _launch_options
        browser = await _ensure_browser(options, restart=restart)
        # Launch-level persistence belongs to Chrome's default context, not an
        # isolated/incognito context that happened to be selected previously.
        pages = [
            tab
            for tab in browser.tabs
            if tab.target.browser_context_id == _default_browser_context_id
        ]
        if not pages:
            tab = await _open_new_tab(
                browser,
                url="about:blank",
                background=False,
                isolated_context="",
                timeout=timeout,
            )
        else:
            tab = next(
                (tab for tab in pages if str(tab.target_id) == _selected_target_id),
                pages[0],
            )
        _selected_target_id = str(tab.target_id)
        await _prepare_page(tab)
        if url:
            _preserve_on_navigation(tab)
            _selected_frames.pop(_selected_target_id, None)
            await tab.send(uc.cdp.page.enable())
            await _auto_enable_network_collection(tab)
            await _await_with_timeout(_navigate_url(tab, url), timeout, "Navigation")
            await _await_with_timeout(tab.wait(), timeout, "Wait for navigation")
        await browser.update_targets()
        return {
            **_browser_launch_info(),
            "status": "restarted"
            if running and changed
            else "reused"
            if running
            else "started",
            "url": tab.target.url,
            "page_id": str(tab.target_id),
            "target_id": str(tab.target_id),
        }


@mcp.tool()
async def close_browser() -> str:
    """Close Chrome, clean temporary profiles and retain persistent profiles for future use.

    Ends the current browser session. The next ordinary browser tool starts a
    fresh temporary session; use start_browser(persistent=true) to reopen a
    persistent one. This does not stop the MCP server itself.
    """
    async with _browser_lock:
        await _shutdown_browser()
    return "Browser closed. Temporary profile removed; persistent profiles retained."


@_page_tool()
async def bypass_insecure_warning(*, page_id: int | str | None = None) -> str:
    """Click through the browser's insecure connection warning page."""
    tab = await _active_tab()
    await tab.bypass_insecure_connection_warning()
    return "Bypassed insecure connection warning."


@_page_tool()
async def cf_verify(*, page_id: int | str | None = None) -> str:
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


@_page_tool()
async def click(
    uid: str,
    dbl_click: bool = False,
    include_snapshot: bool = False,
    *,
    page_id: int | str | None = None,
) -> str:
    """Click a snapshot element with trusted CDP mouse events."""
    try:
        connection = await _tab_for_uid(uid)
        x, y = await _get_box_model(connection, uid)
        await _perform_action(
            connection, lambda: _dispatch_mouse_click(connection, x, y, dbl_click)
        )
        return f"Clicked uid={uid}" + await _maybe_snapshot(include_snapshot)
    except Exception as exc:
        return f"Error clicking uid={uid}: {exc}"


@_page_tool()
async def click_at(
    x: int,
    y: int,
    dbl_click: bool = False,
    include_snapshot: bool = False,
    *,
    page_id: int | str | None = None,
) -> str:
    """Click at specific coordinates on the page.

    Args:
        x: The x coordinate.
        y: The y coordinate.
        dbl_click: Set to true for double clicks.
        include_snapshot: Whether to include a snapshot in the response. Default is false.
    """
    tab = await _active_tab()
    await _perform_action(tab, lambda: _dispatch_mouse_click(tab, x, y, dbl_click))
    result = f"Clicked at ({x}, {y})"
    result += await _maybe_snapshot(include_snapshot)
    return result


@_page_tool()
async def close_page(page_id: int | str | None = None) -> str:
    """Close a page by stable ID or list index, retaining at least one page."""
    browser = await _get_browser()
    tab = await _page_tab()
    key = str(tab.target_id)
    await browser.update_targets()
    if key in _closing_page_ids:
        raise ValueError("This page is already closing.")
    if len([t for t in browser.tabs if str(t.target_id) not in _closing_page_ids]) <= 1:
        return "Error: Cannot close the last open page."
    _closing_page_ids.add(key)
    try:
        trace_info = ""
        if _tracing_active and _trace_tab is tab:
            trace_info = "\n" + await performance_stop_trace()
        await tab.close()
        await _forget_page_state(tab)
        await tab.aclose()
        await browser.update_targets()
    finally:
        _closing_page_ids.discard(key)
    return "Page closed." + await _format_pages() + trace_info


@_page_tool()
async def drag(
    from_uid: str,
    to_uid: str,
    include_snapshot: bool = False,
    *,
    page_id: int | str | None = None,
) -> str:
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
        await _perform_action(
            tab, lambda: tab.mouse_drag((src_x, src_y), (dst_x, dst_y))
        )
        result = f"Dragged uid={from_uid} to uid={to_uid}"
        result += await _maybe_snapshot(include_snapshot)
        return result
    except Exception as e:
        return f"Error dragging: {e}"


@_page_tool()
async def emulate(
    network_conditions: str = "",
    cpu_throttling_rate: float = 0,
    geolocation: str | None = None,
    user_agent: str | None = None,
    color_scheme: str = "",
    viewport: str = "",
    *,
    forced_colors: str = "",
    prefers_contrast: str = "",
    prefers_reduced_motion: str = "",
    extra_http_headers: dict[str, str] | str | None = None,
    page_id: int | str | None = None,
) -> str:
    """Emulates various features on the selected page.

    Args:
        network_conditions: Throttle network. Options: "Offline", "Slow 3G", "Fast 3G", "Slow 4G", "Fast 4G". Omit to disable.
        cpu_throttling_rate: CPU slowdown factor (1-20). Omit or set to 1 to disable.
        geolocation: Geolocation as "latitude,longitude" (e.g. "37.7749,-122.4194"). Omit to leave unchanged, or set to "" to clear.
        user_agent: User agent string. Omit to leave unchanged, or set to "" to clear.
        color_scheme: "dark", "light", or "auto". Empty to skip.
        forced_colors: active, none or auto (reset only this feature).
        prefers_contrast: more, less, custom, no-preference or auto.
        prefers_reduced_motion: reduce, no-preference or auto.
        extra_http_headers: String-valued object (or JSON string); replaces headers, {} or empty string clears. Omit to preserve.
        viewport: Viewport as "widthxheightxdpr[,mobile][,touch][,landscape]" (e.g. "375x812x3,mobile,touch"). Empty to skip.
    """
    tab = await _page_tab()
    results = await _apply_emulation(
        tab,
        network_conditions=network_conditions,
        cpu_throttling_rate=cpu_throttling_rate,
        geolocation=geolocation,
        user_agent=user_agent,
        color_scheme=color_scheme,
        forced_colors=forced_colors,
        prefers_contrast=prefers_contrast,
        prefers_reduced_motion=prefers_reduced_motion,
        extra_http_headers=extra_http_headers,
        viewport=viewport,
    )

    return (
        "Emulation applied: " + ", ".join(results)
        if results
        else "No emulation changes applied."
    )


@_page_tool()
async def reset_emulation(*, page_id: int | str | None = None) -> str:
    """Reset emulation overrides on the selected page back to browser defaults."""
    tab = await _page_tab()
    results = await _reset_emulation(tab)
    return "Emulation reset: " + ", ".join(results)


@_page_tool()
async def emulate_device(
    device: str,
    color_scheme: str = "",
    network_conditions: str = "",
    cpu_throttling_rate: float = 0,
    geolocation: str | None = None,
    *,
    forced_colors: str = "",
    prefers_contrast: str = "",
    prefers_reduced_motion: str = "",
    extra_http_headers: dict[str, str] | str | None = None,
    page_id: int | str | None = None,
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

    tab = await _page_tab()
    device_results = await _apply_device_preset(tab, device)
    extra_results = await _apply_emulation(
        tab,
        network_conditions=network_conditions,
        cpu_throttling_rate=cpu_throttling_rate,
        geolocation=geolocation,
        color_scheme=color_scheme,
        forced_colors=forced_colors,
        prefers_contrast=prefers_contrast,
        prefers_reduced_motion=prefers_reduced_motion,
        extra_http_headers=extra_http_headers,
    )
    return "Emulation applied: " + ", ".join([*device_results, *extra_results])


async def _evaluate_script_value(
    tab, function: str, args: list[str] | None, *, format="auto", timeout=0
):
    return await execute_script(
        tab,
        function,
        args,
        format=format,
        timeout=timeout,
        resolve_context=lambda group: _frame_execution_context(tab, object_group=group),
        resolve_uid=lambda uid, group: _resolve_uid(tab, uid, object_group=group),
    )


@_page_tool(prepare=prepare_script_call)
async def evaluate_script(
    function: str | None = None,
    args: list[str] | None = None,
    *,
    wait_for_stable: bool = True,
    dialog_action: str = "accept",
    prompt_text: str = "",
    timeout: int = 30000,
    format: Literal["auto", "function", "script"] = "auto",
    source_path: str = "",
    service_worker_id: str = "",
    file_path: str = "",
    max_output_chars: int = 20000,
    page_id: int | str | None = None,
) -> str:
    """Evaluate JavaScript with explicit dialog policy and execution status.

    Set wait_for_stable=false for scripts that only read data.
    dialog_action is accept (default), dismiss, or manual; prompt_text supplies
    accepted prompt content. timeout bounds execution in milliseconds; 0 disables it.
    Results report completed, interrupted, failed, or timed_out.
    service_worker_id targets a running worker from list_pages(include_service_workers=true).
    It excludes page_id and element args; worker calls skip DOM/dialog handling.
    Provide exactly one of function (inline) or source_path (local UTF-8 file).
    Paths may be relative, absolute or file: URLs. Source errors fail before page selection.
    format=auto invokes a function result or evaluates an expression/script once;
    function/script choose explicitly. file_path saves complete JSON. Oversized
    inline output is saved automatically; max_output_chars bounds its preview.
    Same-process iframes without explicit Runtime metadata use Document-bound
    evaluation: top-level let/const do not persist between calls. Keep related
    declarations in one call or store deliberate shared state on window. Strict
    eval var/function declarations are also call-local. Do not enable Runtime as
    a workaround; navigating away fails instead of evaluating in a replacement frame.
    """
    try:
        if dialog_action not in {"accept", "dismiss", "manual"}:
            raise ValueError("dialog_action must be accept, dismiss or manual.")
        if timeout < 0:
            raise ValueError("timeout must be nonnegative.")
        if format not in {"auto", "function", "script"}:
            raise ValueError("format must be auto, function or script.")
        if format == "script" and args:
            raise ValueError("Element args require auto or function format.")
        if max_output_chars < 1:
            raise ValueError("max_output_chars must be positive.")
        if service_worker_id:
            if args:
                raise ValueError("Element args cannot be used with service_worker_id.")
            worker = await _worker_targets.get(await _get_browser(), service_worker_id)
            value = await _await_with_timeout(
                execute_script(worker, function, None, format=format, timeout=timeout),
                timeout,
                "Script execution",
            )
            return await script_result(value, file_path, max_output_chars)
        page = await _page_tab()
        existing = _dialog_state.current.get(str(page.target_id))
        if existing is not None:
            if dialog_action == "manual":
                raise DialogInterrupted(existing)
            await _await_with_timeout(
                _dialog_state.handle(existing, dialog_action, prompt_text, page),
                3000,
                "Dialog handling",
            )
            call = _page_call.get()
            if call is not None:
                call.action_results.append(ActionResult(dialogs=[existing]))
        tab = await _tab_for_uid(args[0]) if args else await _active_tab()
        for uid in (args or [])[1:]:
            if await _tab_for_uid(uid) is not tab:
                raise ValueError(
                    "Element arguments must belong to the same frame connection."
                )
        value = await _perform_action(
            tab,
            lambda: _await_with_timeout(
                _evaluate_script_value(
                    tab, function, args, format=format, timeout=timeout
                ),
                timeout,
                "Script execution",
            ),
            wait_for_stable=wait_for_stable,
            dialog_action=dialog_action,
            prompt_text=prompt_text,
        )
        return await script_result(value, file_path, max_output_chars)
    except DialogInterrupted as exc:
        status = "failed" if exc.record.error else "interrupted"
        return f"Status: {status}\n{exc}\nDialog: " + json.dumps(
            exc.record.data(), ensure_ascii=False
        )
    except TimeoutError as exc:
        return f"Error: {exc}\nStatus: timed_out"
    except Exception as exc:
        return f"Error: {exc}\nStatus: failed"


async def _fill_element(element, value: str | bool, uid: str):
    async def click_toggle():
        x, y = await _get_box_model(element.tab, uid)
        await _dispatch_mouse_click(element.tab, x, y, False)

    state = await control_state(element)
    if not state["connected"]:
        raise ValueError("Form control is detached from the current document.")
    if await fill_toggle(element, value, click_toggle, initial=state):
        return
    if not isinstance(value, str):
        raise ValueError("Text inputs and select controls require a string value.")
    await fill_text(element, value, state)


@_page_tool()
async def fill(
    uid: str,
    value: str | StrictBool,
    include_snapshot: bool = False,
    *,
    page_id: int | str | None = None,
) -> str:
    """Fill text/select controls, or set checkbox/radio/switch state idempotently.

    Toggles accept booleans or 'true'/'false'; selected radios cannot be unset.
    """
    try:
        element = await _element_for_uid(uid)
        await _perform_action(element.tab, lambda: _fill_element(element, value, uid))
        return f"Filled uid={uid}" + await _maybe_snapshot(include_snapshot)
    except Exception as exc:
        return f"Error filling uid={uid}: {exc}"


@_page_tool()
async def fill_form(
    elements: list[dict],
    include_snapshot: bool = False,
    *,
    page_id: int | str | None = None,
) -> str:
    """Fill a batch with one frame refresh/wait; stop on its first interruption.

    Report completed fields, the failed/interrupted UID and skipped entries.
    """
    if not elements:
        return "Form fill results:\nCompleted 0 of 0 fields."
    lines, completed = [], []
    current_uid, failed_uid, stop_reason = None, None, None
    current_completed = False

    def failed(reason):
        nonlocal failed_uid, stop_reason
        stop_reason = f"Error: {reason}"
        if current_uid is not None and not current_completed and failed_uid is None:
            failed_uid = current_uid
            lines.append(f"  uid={current_uid}: Error: {reason}")

    try:
        page = await _page_tab()
        await _discover_frames(page)
        selected = _selected_frame_id(page)
        tab = (
            _frame_connections.get((str(page.target_id), selected))
            if selected
            else page
        )
        if tab is None:
            raise ValueError("Selected frame no longer exists.")

        async def apply_entries():
            nonlocal current_uid, stop_reason, current_completed
            call = _page_call.get()
            if call is not None and call.waiter is not None:
                call.waiter.frame_ids.update(
                    owner[2]
                    for entry in elements
                    if (owner := _uid_owners.get(entry.get("uid", "")))
                    and owner[0] == str(page.target_id)
                    and owner[2]
                )
            for entry in elements:
                current_uid = entry.get("uid", "")
                current_completed = False
                try:
                    if not isinstance(current_uid, str) or not current_uid:
                        raise ValueError("Each form entry requires a nonempty uid.")
                    element = await _element_for_uid(current_uid, refresh=False)
                    await _fill_element(element, entry.get("value", ""), current_uid)
                    completed.append(current_uid)
                    current_completed = True
                    lines.append(f"  uid={current_uid}: filled")
                    if (
                        call is not None
                        and call.waiter is not None
                        and call.waiter.navigating.is_set()
                    ):
                        stop_reason = f"Navigation started after uid={current_uid}."
                        return
                    dialog = _dialog_state.current.get(str(page.target_id))
                    if dialog is not None:
                        raise DialogInterrupted(dialog)
                except Exception as exc:
                    failed(exc)
                    return
            current_uid = None

        await _perform_action(tab, apply_entries)
    except Exception as exc:
        failed(exc)
    skipped = len(elements) - len(completed) - (1 if failed_uid is not None else 0)
    result = (
        f"Form fill results:\nCompleted {len(completed)} of {len(elements)} fields.\n"
        + "\n".join(lines)
    )
    if stop_reason:
        result += (
            f"\nStopped: {stop_reason}\nSkipped {max(0, skipped)} remaining fields."
        )
    if include_snapshot:
        try:
            result += await _maybe_snapshot(True)
        except Exception as exc:
            result += f"\nSnapshot unavailable: {exc}"
    return result


async def _console_target(service_worker_id="", *, root=False):
    if service_worker_id:
        return await _worker_targets.get(await _get_browser(), service_worker_id)
    return await _page_tab() if root else await _active_tab()


@_page_tool()
async def get_console_message(
    msgid: int,
    *,
    include_stack_traces: bool = True,
    resolve_source_maps: bool = True,
    service_worker_id: str = "",
    page_id: int | str | None = None,
) -> str:
    """Get console/exception details with generated and original source locations.

    resolve_source_maps uses mappings captured by enable_console_collection(source_maps=true);
    this lookup never enables Debugger implicitly. False keeps only generated locations.
    """
    page_id = _page_key(await _console_target(service_worker_id, root=True))
    records = _console_messages + [
        m for batch in _preserved_console_messages for m in batch
    ]
    message = next(
        (m for m in records if m["msgid"] == msgid and m["page_id"] == page_id), None
    )
    if message is None:
        return f"Error: Unknown msgid {msgid} for this page."
    message = await symbolize_message(message, resolve_source_maps)
    return format_message(message, include_stack_traces=include_stack_traces)


@_page_tool()
async def get_cookies(url: str = "", *, page_id: int | str | None = None) -> str:
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


@_page_tool()
async def get_local_storage(*, page_id: int | str | None = None) -> str:
    """Get all localStorage items from the current page."""
    tab = await _active_tab()
    data = await tab.get_local_storage()
    lines = ["localStorage items:"]
    for k, v in (data or {}).items():
        lines.append(f"  {k}: {str(v)[:200]}")
    return "\n".join(lines)


@_page_tool()
async def get_network_request(
    reqid: int | None = None,
    request_file_path: str = "",
    response_file_path: str = "",
    *,
    include_body: bool = True,
    max_body_chars: int = 5000,
    page_id: int | str | None = None,
) -> str:
    """Read headers, redirect hops and body states using the originating connection.

    include_body skips body lookups unless exporting; max_body_chars bounds each
    inline preview. File exports always retain the complete body.
    """
    if max_body_chars < 1:
        raise ValueError("max_body_chars must be positive.")
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
    return await request_details(
        req,
        request_file_path,
        response_file_path,
        include_body=include_body,
        max_body_chars=max_body_chars,
    )


@_page_tool()
async def handle_dialog(
    action: str = "accept", prompt_text: str = "", *, page_id: int | str | None = None
) -> str:
    """If a browser dialog was opened, use this command to handle it.

    Args:
        action: Whether to "accept" or "dismiss" the dialog.
        prompt_text: Optional text to enter into a prompt dialog.
    """
    if action not in {"accept", "dismiss"}:
        raise ValueError("action must be accept or dismiss.")
    tab = await _page_tab()
    import nodriver.cdp.page as cdp_page

    existing = _dialog_state.current.get(str(tab.target_id))
    if existing is not None:
        await _await_with_timeout(
            _dialog_state.handle(existing, action, prompt_text, tab),
            3000,
            "Dialog handling",
        )
    elif action == "accept":
        await tab.send(
            cdp_page.handle_java_script_dialog(accept=True, prompt_text=prompt_text)
        )
    else:
        await tab.send(cdp_page.handle_java_script_dialog(accept=False))
    return f"Dialog {action}ed."


@_page_tool()
async def hover(
    uid: str, include_snapshot: bool = False, *, page_id: int | str | None = None
) -> str:
    """Hover over the provided element.

    Args:
        uid: The uid of an element on the page from the page content snapshot.
        include_snapshot: Whether to include a snapshot in the response. Default is false.
    """
    tab = await _tab_for_uid(uid)
    try:
        cx, cy = await _get_box_model(tab, uid)
        await _perform_action(tab, lambda: tab.mouse_move(cx, cy))
        result = f"Hovered over uid={uid}"
        result += await _maybe_snapshot(include_snapshot)
        return result
    except Exception as e:
        return f"Error hovering uid={uid}: {e}"


@_page_tool()
async def list_console_messages(
    page_size: int | None = None,
    page_idx: int = 0,
    types: list[str] | None = None,
    include_preserved_messages: bool = False,
    *,
    include_stack_traces: bool = False,
    resolve_source_maps: bool = True,
    service_worker_id: str = "",
    page_id: int | str | None = None,
) -> str:
    """List console calls and uncaught JS exceptions, optionally including stacks.

    resolve_source_maps symbolizes requested stacks when source_maps collection was enabled.
    Failed maps retain generated positions and a diagnostic; Debugger is opt-in.
    """
    tab = await _console_target(service_worker_id)
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
    records = await symbolize_messages(
        records, resolve_source_maps and include_stack_traces
    )
    lines = []
    for message in records:
        line = f"  [{message['msgid']}] [{message['type']}] {message['text'][:200]}"
        if include_stack_traces:
            stack = format_stack(message.get("stack_trace"))
            if stack:
                line += "\n" + stack
        if include_stack_traces and message.get("source_map_errors"):
            line += "\nSource map: " + "; ".join(message["source_map_errors"])
        lines.append(line)
    return f"Console messages ({len(records)} of {total}):\n" + "\n".join(lines)


@_page_tool(
    description=(
        "Enable page or service_worker_id console collection (optional console tool group). "
        "This calls Runtime.enable. "
        + RUNTIME_ENABLE_WARNING
        + " source_maps=true additionally enables Debugger, async stacks and source-map "
        "symbolization while skipping debugger pauses. Omit source_maps to preserve "
        "its setting; false disables owned Debugger collection."
    )
)
async def enable_console_collection(
    *,
    source_maps: bool | None = None,
    service_worker_id: str = "",
    page_id: int | str | None = None,
) -> str:
    """Enable page or service_worker_id console collection; source_maps=true opts into Debugger symbolization.

    This enables async stack capture and skips debugger pauses. Omit source_maps
    to preserve its setting, or use false to disable owned Debugger collection.
    """
    tab = await _console_target(service_worker_id)
    changed = await _enable_console_collection(tab, source_maps)
    label = (
        f"service worker {service_worker_id}" if service_worker_id else "current page"
    )
    if changed:
        return f"Console collection enabled on the {label}."
    return f"Console collection was already enabled on the {label}."


@_page_tool()
async def disable_console_collection(
    *, service_worker_id: str = "", page_id: int | str | None = None
) -> str:
    """Disable console event collection on the page or specified service_worker_id."""
    tab = await _console_target(service_worker_id)
    changed = await _disable_console_collection(tab)
    label = (
        f"service worker {service_worker_id}" if service_worker_id else "current page"
    )
    if changed:
        return f"Console collection disabled on the {label}."
    return f"Console collection was already disabled on the {label}."


@_page_tool()
async def list_network_requests(
    page_size: int | None = None,
    page_idx: int = 0,
    resource_types: list[str] | None = None,
    url_filter: str = "",
    include_preserved_requests: bool = False,
    *,
    page_id: int | str | None = None,
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
        f"  [{r['reqid']}] {r['method']} {r['url'][:150]} ({r['type']}, status={r.get('status', r.get('error', 'pending'))})"
        for r in records
    )


@mcp.tool()
async def list_pages(include_service_workers: bool = False) -> str:
    """List stable page IDs; optionally discover running service_worker_id targets.

    Workers include extension ID/URL when applicable. Only running workers appear;
    stopped IDs fail explicitly. Worker selection never changes the selected page.
    """
    result = (await _format_pages()).lstrip()
    if include_service_workers:
        workers = await _worker_targets.discover(await _get_browser())
        result += "\nRunning service workers:\n" + "\n".join(
            "  " + json.dumps(worker_info(worker), ensure_ascii=False)
            for worker in workers
        )
    return result


@_page_tool()
async def navigate_page(
    type: str = "url",
    url: str = "",
    ignore_cache: bool = False,
    handle_before_unload: str = "accept",
    init_script: str = "",
    timeout: int = 0,
    device: str = "",
    *,
    page_id: int | str | None = None,
) -> str:
    """Navigate the selected page. Optional device emulation runs before navigation.

    For a user-requested proxy or persistent session, first use start_browser.
    Otherwise first use starts a temporary browser; subsequent calls keep its session.
    """
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
        await _perform_action(
            tab,
            lambda: _await_with_timeout(actions[type](), timeout, "Navigation"),
            preserve_navigation=False,
            ignored_dialogs=frozenset({"beforeunload"}),
        )
        pages = await _format_pages()
        return f"Navigated to {tab.target.url}" + pages
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
    """Open a page, optionally in a named isolated context, with device settings before its first request.

    For a user-requested proxy or persistent session, use start_browser(url=...)
    first. Otherwise first use starts a temporary browser. An isolated_context is
    an incognito context, not a persistent profile; use start_browser's profile instead.
    """
    global _selected_target_id
    if device and _resolve_device_preset(device) is None:
        return f"Error: Unknown device preset '{device}'."
    browser = await _get_browser()
    try:
        tab = await _open_new_tab(
            browser,
            url="about:blank",
            background=background,
            isolated_context=isolated_context,
            timeout=timeout,
        )
        await _prepare_page(tab)
        await _auto_enable_network_collection(tab)
        if device:
            await _apply_device_preset(tab, device)
        if url != "about:blank":
            await _perform_action(
                tab,
                lambda: _await_with_timeout(
                    _navigate_url(tab, url), timeout, "Navigate new page"
                ),
                preserve_navigation=False,
            )
        if not background:
            _selected_target_id = str(tab.target_id)
            await tab.activate()
        pages = await _format_pages()
        return f"Opened new page: {tab.target.url} (page_id={tab.target_id})" + pages
    except Exception as exc:
        return f"Error opening new page: {exc}"


@_page_tool()
async def performance_start_trace(
    reload: bool = True,
    auto_stop: bool = True,
    file_path: str = "",
    *,
    duration_ms: int = 5000,
    categories: list[str] | None = None,
    ignore_cache: bool = False,
    analyze: bool = False,
    page_id: int | str | None = None,
) -> str:
    """Record a native trace, always preserving its complete raw file on stop.

    auto_stop records for duration_ms (default 5000). categories overrides native
    DevTools defaults. ignore_cache applies to reload. analyze=true appends native
    DevTools Insights/Web Vitals (optional Node.js runtime). Manual stop retains the
    startup file_path when no replacement path is supplied. JSON/gzip are supported.
    """
    global _tracing_active, _trace_tab, _trace_metadata
    if (
        duration_ms < 1
        or categories == []
        or (categories and any(not c.strip() for c in categories))
    ):
        raise ValueError(
            "duration_ms must be positive and categories must be nonempty."
        )
    tab = await _page_tab()
    if _tracing_active:
        return "Error: A browser-wide trace is already running."
    state = {
        "started": time.monotonic(),
        "file_path": file_path,
        "config": {
            "page_id": str(tab.target_id),
            "url": tab.target.url,
            "categories": list(categories or TRACE_CATEGORIES),
            "reload": reload,
            "auto_stop": auto_stop,
            "duration_ms": duration_ms,
            "ignore_cache": ignore_cache,
            "analyze": analyze,
        },
    }
    _tracing_active, _trace_tab, _trace_metadata = True, tab, state
    started = False
    try:
        await tab.send(
            uc.cdp.tracing.start(
                categories=",".join(state["config"]["categories"]),
                transfer_mode="ReturnAsStream",
            )
        )
        started = True
        if reload:
            await tab.send(uc.cdp.page.reload(ignore_cache=ignore_cache))
        if auto_stop:
            await asyncio.sleep(duration_ms / 1000)
            if state.get("result"):
                return state["result"]
            if _trace_metadata is not state:
                raise ValueError("This recording is no longer active.")
            return await performance_stop_trace(file_path, analyze=analyze)
        return "Trace started. Use performance_stop_trace to stop.\n" + json.dumps(
            state["config"], ensure_ascii=False
        )
    except BaseException:
        if _trace_metadata is state and not _trace_stopping:
            if started:
                try:
                    await asyncio.shield(
                        asyncio.wait_for(
                            performance_stop_trace(file_path, analyze=False), 35
                        )
                    )
                except BaseException:
                    logger.debug("Trace cleanup failed", exc_info=True)
            if _trace_metadata is state and not _trace_stopping:
                _tracing_active, _trace_tab, _trace_metadata = False, None, {}
        raise


@_page_tool()
async def performance_stop_trace(
    file_path: str = "",
    *,
    analyze: bool | None = None,
    page_id: int | str | None = None,
) -> str:
    """Finish via the original connection and save all raw events to JSON or gzip.

    Returns absolute file_path, recording duration, categories/config and Chrome's
    data-loss flag. An omitted path uses the start path or persistent artifacts/traces.
    analyze=true appends real DevTools Insights; omit to inherit the start setting.
    Raw data is preserved and recording state released even if analysis is unavailable.
    """
    global \
        _tracing_active, \
        _trace_tab, \
        _trace_stopping, \
        _trace_metadata, \
        _last_trace_path
    if not _tracing_active or _trace_tab is None:
        return "Error: No trace is running."
    tab, state = _trace_tab, _trace_metadata
    if page_id is not None and tab is not await _page_tab():
        raise ValueError("The active trace belongs to a different page_id.")
    if _trace_metadata is not state:
        raise ValueError("This recording is no longer active.")
    if _trace_stopping:
        raise ValueError("The active trace is already stopping.")
    _trace_stopping = True
    done = asyncio.get_running_loop().create_future()

    def complete(event):
        if not done.done():
            done.set_result(event)

    tab.add_handler(uc.cdp.tracing.TracingComplete, complete)
    try:
        await tab.send(uc.cdp.tracing.end())
        event = await asyncio.wait_for(done, 30)
        elapsed = (time.monotonic() - state.get("started", time.monotonic())) * 1000
        if event.stream is None:
            raise ValueError("Chrome did not return a trace stream.")
        data = await _read_stream(tab, event.stream)
        result = await save_trace(
            data,
            file_path or state.get("file_path", ""),
            max(0, elapsed),
            state.get("config", {}),
            getattr(event, "data_loss_occurred", False),
        )
        _last_trace_path = result["file_path"]
        report = (
            f"Trace stopped. {result['event_count']} events collected.\n"
            + json.dumps(result, ensure_ascii=False, indent=2)
        )
        state["result"] = report
    finally:
        tab.remove_handler(uc.cdp.tracing.TracingComplete, complete)
        if not done.done():
            done.cancel()
        if _trace_metadata is state:
            _tracing_active, _trace_tab, _trace_metadata = False, None, {}
            _trace_stopping = False
    should_analyze = (
        state.get("config", {}).get("analyze", False) if analyze is None else analyze
    )
    if should_analyze:
        try:
            report += "\n\n" + await performance_analyze_trace(
                trace_path=result["file_path"]
            )
        except (ValueError, TimeoutError) as exc:
            report += "\nAnalysis unavailable: " + str(exc)
    state["result"] = report
    return report


@mcp.tool()
async def performance_analyze_trace(
    trace_path: str = "",
    file_path: str = "",
    max_output_chars: int = 20000,
    timeout: int = 60000,
) -> str:
    """Analyze a saved JSON/gzip trace with the actual upstream DevTools engine.

    Omit trace_path to use the latest recording. Returns observed Web Vitals and
    native Insights with IDs/names for performance_analyze_insight. Always saves
    complete JSON analysis; max_output_chars bounds the inline preview. Runs offline
    without launching Chrome. Requires Node.js 22.12+; downloads a pinned, verified
    engine package into a local cache on first use. timeout is ms; 0 disables it.
    """
    source = trace_path or _last_trace_path
    if not source:
        raise ValueError("Provide trace_path or record a trace first.")
    return await analyze_trace_file(
        source, file_path=file_path, max_output_chars=max_output_chars, timeout=timeout
    )


@mcp.tool()
async def performance_analyze_insight(
    insight_set_id: str,
    insight_name: str,
    trace_path: str = "",
    file_path: str = "",
    max_output_chars: int = 20000,
    timeout: int = 60000,
) -> str:
    """Inspect one native DevTools Insight, including detailed causes and savings.

    Use insight_set_id and insight_name from performance_analyze_trace. Reads the
    same saved trace (latest recording when omitted) without opening Chrome.
    Complete analysis is saved to file_path or an automatic persistent artifact.
    """
    source = trace_path or _last_trace_path
    if not source:
        raise ValueError("Provide trace_path or record a trace first.")
    return await analyze_trace_file(
        source,
        insight_set_id=insight_set_id,
        insight_name=insight_name,
        file_path=file_path,
        max_output_chars=max_output_chars,
        timeout=timeout,
    )


async def _cleanup_trace():
    if _tracing_active and not _trace_stopping:
        await asyncio.wait_for(performance_stop_trace(analyze=False), 35)


@_page_tool()
async def press_key(
    key: str, include_snapshot: bool = False, *, page_id: int | str | None = None
) -> str:
    """Press a key or key combination.

    Args:
        key: A key or combination (e.g. "Enter", "Control+A", "Control++", "Control+Shift+R").
            Modifiers: Control, Shift, Alt, Meta.
        include_snapshot: Whether to include a snapshot in the response. Default is false.
    """
    tokens = parse_shortcut(key)
    tab = await _active_tab()
    await _perform_action(tab, lambda: dispatch_shortcut(tab, tokens))

    result = f"Pressed {key}"
    result += await _maybe_snapshot(include_snapshot)
    return result


@_page_tool()
async def resize_page(
    width: int, height: int, *, page_id: int | str | None = None
) -> str:
    """Resizes the selected page's window so that the page has specified dimension.

    Args:
        width: Page width in pixels.
        height: Page height in pixels.
    """
    tab = await _active_tab()
    await tab.set_window_size(width=width, height=height)
    return f"Resized to {width}x{height}."


@_page_tool()
async def scroll_page(
    direction: str = "down", amount: int = 50, *, page_id: int | str | None = None
) -> str:
    """Scroll the page up or down.

    Args:
        direction: "up" or "down".
        amount: Percentage of page to scroll (25 = quarter page).
    """
    tab = await _active_tab()
    await _perform_action(
        tab,
        lambda: (
            tab.scroll_down(amount) if direction == "down" else tab.scroll_up(amount)
        ),
    )
    return f"Scrolled {direction} {amount}%."


@_page_tool()
async def select_page(page_id: int | str, bring_to_front: bool = True) -> str:
    """Select a page as the persistent context for subsequent tools."""
    global _selected_target_id
    tab = await _page_tab()
    _selected_target_id = str(tab.target_id)
    _select_frame(tab, None)
    await _auto_enable_network_collection(tab)
    if bring_to_front:
        await tab.activate()
    pages = await _format_pages()
    return f"Selected page [{page_id}]: {tab.target.url}" + pages


@_page_tool()
async def set_cookie(
    name: str,
    value: str,
    domain: str,
    path: str = "/",
    secure: bool = False,
    *,
    page_id: int | str | None = None,
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


@_page_tool()
async def set_local_storage(
    items: dict[str, str], *, page_id: int | str | None = None
) -> str:
    """Set localStorage items on the current page.

    Args:
        items: Dict of key-value pairs to set in localStorage.
    """
    tab = await _active_tab()
    await tab.set_local_storage(items)
    return f"Set {len(items)} localStorage items."


@_page_tool()
async def take_memory_snapshot(
    file_path: str, *, page_id: int | str | None = None
) -> str:
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


@_page_tool()
async def take_screenshot(
    full_page: bool = False,
    format: str = "png",
    quality: int = 0,
    uid: str = "",
    file_path: str = "",
    *,
    max_width: int | None = None,
    max_height: int | None = None,
    page_id: int | str | None = None,
) -> Any:
    """Capture an MCP image or save a file. Ordinary PNG/JPEG uses native as_base64.

    Element crops, WebP and explicit quality use official CDP because the native
    screenshot helper does not expose those arguments. max_width/max_height bound
    physical output pixels proportionally, without upscaling.
    """
    if uid and full_page:
        raise ValueError("Cannot combine uid and full_page.")
    if format not in {"png", "jpeg", "jpg", "webp"} or not 0 <= quality <= 100:
        raise ValueError("Invalid format or quality.")
    if any(v is not None and v < 1 for v in (max_width, max_height)):
        raise ValueError("max_width and max_height must be positive when specified.")
    limited = max_width is not None or max_height is not None
    format = "jpeg" if format == "jpg" else format
    tab = await _tab_for_uid(uid) if uid else await _active_tab()
    if not limited and not uid and format in {"png", "jpeg"} and not quality:
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
        if uid or limited:
            kwargs["clip"] = await screenshot_clip(
                tab,
                _uid_to_backend_node_id[uid] if uid else None,
                full_page,
                max_width,
                max_height,
            )
            kwargs["capture_beyond_viewport"] = True
        encoded = await tab.send(uc.cdp.page.capture_screenshot(**kwargs))
        if file_path:
            Path(file_path).write_bytes(base64.b64decode(encoded))
            return f"Screenshot saved to {file_path}."
    return Image(data=base64.b64decode(encoded), format=format)


@_page_tool()
async def take_snapshot(
    verbose: bool = False,
    file_path: str = "",
    include_frames: bool = False,
    *,
    page_id: int | str | None = None,
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

    selected_frame = _selected_frame_id(page)
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


@_page_tool()
async def type_text(
    text: str, submit_key: str = "", *, page_id: int | str | None = None
) -> str:
    """Insert Unicode text into the focused control using official CDP."""
    tab = await _active_tab()

    async def insert():
        await tab.send(uc.cdp.input_.insert_text(text))
        if submit_key:
            await press_key(submit_key)

    await _perform_action(tab, insert)
    return f"Typed {len(text)} characters" + (
        f", then pressed {submit_key}" if submit_key else ""
    )


@_page_tool()
async def upload_file(
    uid: str,
    file_path: str = "",
    include_snapshot: bool = False,
    *,
    file_paths: list[str] | None = None,
    timeout: int = 3000,
    page_id: int | str | None = None,
) -> str:
    """Upload local files through an input or a button that opens a file chooser.

    Use file_path for one file or file_paths for one/many files. Native file
    inputs use Element.send_file; buttons use official Page/DOM chooser commands.
    timeout bounds waiting for the chooser in milliseconds; 0 disables it.
    """
    paths = upload_paths(file_path, file_paths)
    if timeout < 0:
        raise ValueError("timeout must be nonnegative.")
    element = await _element_for_uid(uid)
    page = await _page_tab()

    async def click_button():
        x, y = await _get_box_model(element.tab, uid)
        await _dispatch_mouse_click(element.tab, x, y, False)

    async def chooser_frame(frame_id):
        await _discover_frames(page)
        connection = _frame_connections.get((str(page.target_id), frame_id))
        if connection is None:
            raise ValueError("File chooser frame no longer exists.")
        return connection

    await _perform_action(
        element.tab,
        lambda: _file_uploads.upload(
            element,
            page,
            paths,
            click_button,
            chooser_frame,
            timeout,
        ),
    )
    return f"Uploaded {', '.join(paths)} to uid={uid}" + await _maybe_snapshot(
        include_snapshot
    )


@_page_tool()
async def wait_for(
    text: list[str], timeout: int = 30000, *, page_id: int | str | None = None
) -> str:
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


@_page_tool()
async def save_session(name: str, *, page_id: int | str | None = None) -> str:
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


@_page_tool()
async def load_session(
    filename: str, restore_pages: bool = False, *, page_id: int | str | None = None
) -> str:
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
_shutdown_callbacks.append(_cleanup_trace)


if __name__ == "__main__":
    main()
