"""Additional native nodriver tools and complete generated CDP access."""

from __future__ import annotations

import asyncio
import inspect
import itertools
from collections import deque
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import nodriver as uc
from pydantic import StrictBool

from .protocol import (
    build_command,
    command_function,
    describe_commands,
    domain_module,
    event_types,
    json_value,
    resolve_event,
)
from .runtime_policy import RUNTIME_ENABLE_WARNING, require_runtime_enable
from .scripts import script_error


_SERVICE_WORKER_ROUTER_NOTE = (
    "Known limitation in official nodriver 0.50.6: "
    "ServiceWorker.workerVersionUpdated can lose recursive 'or'/'not' conditions "
    "in typedRouterRules during parsing. Treat these reported conditions as "
    "potentially incomplete; an empty condition {} does not prove an unconditional "
    "match. Do not infer a website's cache/network routing solely from these "
    "records; check actual request behavior. Chrome's own routing is unaffected."
)


async def _prepare_cdp_command(arguments):
    domain = arguments["domain"]
    command = command_function(domain, arguments["command"])
    if domain_module(domain) is uc.cdp.runtime and command.__name__ == "enable":
        require_runtime_enable(arguments.get("allow_runtime_enable", False))


async def _prepare_cdp_subscription(arguments):
    parsed = [resolve_event(name) for name in arguments["events"]]
    if arguments.get("enable_domain", True) and any(
        cls.__module__ == uc.cdp.runtime.__name__ for _, cls in parsed
    ):
        require_runtime_enable(arguments.get("allow_runtime_enable", False))


async def _browser_version_page(browser) -> dict[str, str]:
    """Read a temporary version page without retaining DOM node handles."""
    creation = asyncio.create_task(
        browser.send(uc.cdp.target.create_target("chrome://version/", background=True))
    )
    target_id = None
    tab = None
    try:
        async with asyncio.timeout(10):
            target_id = await asyncio.shield(creation)
            tab = uc.Tab(target=target_id, parent=browser, auto_attach=False)
            while True:
                result, error = await tab.send(
                    uc.cdp.runtime.evaluate(
                        """Object.fromEntries(Array.from(document.querySelectorAll('tr'))
                            .filter(row => row.children.length >= 2)
                            .map(row => [row.children[0].innerText.trim(),
                                         row.children[1].innerText.trim()]))""",
                        return_by_value=True,
                    )
                )
                if error:
                    raise script_error(error)
                if result.value:
                    return result.value
                await asyncio.sleep(0.05)
    except TimeoutError as exc:
        raise TimeoutError("Reading chrome://version timed out after 10000ms") from exc
    finally:
        try:
            # Retrieving a late creation reply and closing its target are one
            # bounded cleanup operation, even inside a cancelled MCP scope.
            with anyio.move_on_after(5, shield=True) as cleanup:
                if target_id is None:
                    target_id = await asyncio.shield(creation)
                await browser.send(uc.cdp.target.close_target(target_id))
                while any(
                    target.target_id == target_id
                    for target in await browser.send(uc.cdp.target.get_targets())
                ):
                    await asyncio.sleep(0.05)
            if cleanup.cancel_called:
                raise TimeoutError(
                    "Temporary chrome://version page did not close within 5000ms"
                )
        finally:
            if not creation.done():
                creation.cancel()
            with anyio.move_on_after(0.5, shield=True):
                await asyncio.gather(creation, return_exceptions=True)
            if tab is not None:
                with anyio.move_on_after(0.5, shield=True):
                    await tab.aclose()


async def _extension_tab_target(browser, page):
    """Resolve a renderer's owning tab without matching ambiguous URLs/titles."""
    targets = await browser.send(
        uc.cdp.target.get_targets(
            filter_=uc.cdp.target.TargetFilter([{"type": "tab"}, {"exclude": True}])
        )
    )
    for target in targets:
        if target.browser_context_id != page.target.browser_context_id:
            continue
        guard = uc.Tab(target=target, parent=browser, auto_attach=False)
        children = []
        guard.add_handler(uc.cdp.target.AttachedToTarget, children.append)
        try:
            # Auto-attach replays the tab's existing renderers before replying.
            # The separate connection leaves page/frame collectors untouched.
            await guard.send(
                uc.cdp.target.set_auto_attach(
                    True,
                    wait_for_debugger_on_start=False,
                    flatten=True,
                    filter_=uc.cdp.target.TargetFilter(
                        [{"type": "page"}, {"exclude": True}]
                    ),
                )
            )
            if any(event.target_info.target_id == page.target_id for event in children):
                return target.target_id
        finally:
            await guard.aclose()
    raise ValueError("No owning tab target found for this page; use list_pages.")


async def _frame_xpath(connection, frame, expression, timeout, limit):
    """Evaluate XPath against a same-process frame's own Document."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        group = "nodriver-mcp-xpath-" + uuid4().hex
        try:
            document = await connection.send(
                uc.cdp.dom.resolve_node(
                    backend_node_id=frame.content_document.backend_node_id,
                    object_group=group,
                )
            )
            result, error = await connection.send(
                uc.cdp.runtime.call_function_on(
                    """function(expression, limit) {
                        const result = this.evaluate(expression, this, null, 7, null);
                        const nodes = [];
                        for (let i=0; i<result.snapshotLength && nodes.length<limit; i++) {
                            const item = result.snapshotItem(i);
                            const node = item.nodeType === 1 ? item : item.parentElement || item.ownerElement;
                            if (node) nodes.push(node);
                        }
                        return nodes;
                    }""",
                    object_id=document.object_id,
                    arguments=[
                        uc.cdp.runtime.CallArgument(value=expression),
                        uc.cdp.runtime.CallArgument(value=limit),
                    ],
                    object_group=group,
                )
            )
            if error:
                raise script_error(error)
            properties, *_ = await connection.send(
                uc.cdp.runtime.get_properties(result.object_id, own_properties=True)
            )
            elements = []
            for prop in properties:
                if prop.name.isdecimal() and prop.value and prop.value.object_id:
                    node = await connection.send(
                        uc.cdp.dom.describe_node(
                            object_id=prop.value.object_id, depth=-1, pierce=True
                        )
                    )
                    elements.append(uc.Element(node, connection))
            if elements or asyncio.get_running_loop().time() >= deadline:
                return elements
        finally:
            with anyio.move_on_after(0.5, shield=True):
                try:
                    await connection.send(uc.cdp.runtime.release_object_group(group))
                except Exception:
                    pass
        await asyncio.sleep(0.05)


def register_tools(mcp, s) -> None:
    """Register tools with the server's shared page/frame lifecycle services."""
    subscriptions: dict[str, dict] = {}
    subscription_ids = itertools.count(1)
    web_states: dict[int, dict] = {}
    recordings: dict[str, dict] = {}
    extension_actions = asyncio.Lock()

    async def connection(scope: str = "page"):
        if scope == "browser":
            return await s._get_browser()
        if scope == "page":
            return await s._active_tab()
        raise ValueError("scope must be page or browser")

    async def cleanup():
        for entry in subscriptions.values():
            for event, callback in entry["callbacks"]:
                entry["connection"].remove_handler(event, callback)
        subscriptions.clear()
        for state in web_states.values():
            for event, callback in state["callbacks"]:
                state["connection"].remove_handler(event, callback)
            for future in state["pending"].values():
                if not future.done():
                    future.cancel()
        web_states.clear()
        recordings.clear()

    async def cleanup_page(page_id):
        for identifier, entry in list(subscriptions.items()):
            if entry["page_id"] == page_id:
                for event, callback in entry["callbacks"]:
                    entry["connection"].remove_handler(event, callback)
                del subscriptions[identifier]
        for key, state in list(web_states.items()):
            if s._page_key(state["connection"]) == page_id:
                for event, callback in state["callbacks"]:
                    state["connection"].remove_handler(event, callback)
                for future in state["pending"].values():
                    if not future.done():
                        future.cancel()
                del web_states[key]
        recordings.pop(page_id, None)

    s._shutdown_callbacks.append(cleanup)
    s._page_cleanup_callbacks.append(cleanup_page)

    @mcp.tool()
    async def get_browser_info(include_version_page: bool = False) -> dict:
        """Get native browser/protocol versions, optionally chrome://version details."""
        browser = await s._get_browser()
        version = await browser.send(uc.cdp.browser.get_version())
        result = dict(
            zip(
                ("protocol_version", "product", "revision", "user_agent", "js_version"),
                version,
            )
        )
        result["nodriver_version"] = uc.__version__
        result["launch"] = s._browser_launch_info()
        if include_version_page:
            result["version_page"] = await _browser_version_page(browser)
        return result

    @s._page_tool(
        description=(
            "Read native recent transactions (up to nodriver's 25 retained entries).\n\n"
            + _SERVICE_WORKER_ROUTER_NOTE
        )
    )
    async def get_connection_history(
        limit: int = 10, scope: str = "page", *, page_id: int | str | None = None
    ) -> list[dict]:
        """Read native recent transactions (up to nodriver's 25 retained entries)."""
        if not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25")
        tab = await connection(scope)
        return [
            {
                "id": tx.id,
                "request": json_value(tx.request),
                "result": str(tx.result)
                if isinstance(tx.result, Exception)
                else json_value(tx.result),
                "events": json_value(tx.events),
            }
            for tx in tab.transactions[-limit:]
        ]

    @s._page_tool()
    async def list_frames(*, page_id: int | str | None = None) -> list[dict]:
        """List same-process and connectable iframe sessions using native get_frames."""
        page = await s._page_tab()
        return [
            {
                "frame_id": str(frame.id_),
                "parent_id": str(frame.parent_id) if frame.parent_id else None,
                "url": frame.url,
                "name": frame.name,
                "separate_connection": tab is not page,
                "selected": s._selected_frame_id(page) == str(frame.id_),
            }
            for frame, tab in await s._discover_frames(page)
        ]

    @s._page_tool()
    async def select_frame(
        frame_id: str = "", *, page_id: int | str | None = None
    ) -> str:
        """Select a frame for snapshots/scripts, or pass empty to return to the main page."""
        page = await s._page_tab()
        if not frame_id:
            s._select_frame(page, None)
            return "Selected main frame."
        frames = await s._discover_frames(page)
        frame = next((frame for frame, _ in frames if str(frame.id_) == frame_id), None)
        if frame is None:
            raise ValueError("Unknown frame_id; use list_frames first.")
        if frame_id == str(frames[0][0].id_):
            s._select_frame(page, None)
            return "Selected main frame."
        s._select_frame(page, frame_id)
        return f"Selected frame {frame_id}."

    @s._page_tool()
    async def find_elements(
        text: str = "",
        selector: str = "",
        xpath: str = "",
        timeout: float = 10,
        limit: int = 100,
        best_match: bool = False,
        *,
        page_id: int | str | None = None,
    ) -> list[dict]:
        """Use native find_all/select_all/xpath and return actionable element uids."""
        if sum(bool(value) for value in (text, selector, xpath)) != 1:
            raise ValueError("Supply exactly one of text, selector or xpath.")
        if not 1 <= limit <= 1000 or timeout < 0:
            raise ValueError("Invalid limit or timeout.")
        if best_match and not text:
            raise ValueError("best_match applies to text search.")
        tab = await s._active_tab()
        page = await s._page_tab()
        frame_id = s._selected_frame_id(page)
        frame_element = (
            await s._frame_element(tab, frame_id)
            if frame_id and frame_id != str(tab.target_id)
            else None
        )
        if text:
            if best_match and frame_element is None:
                match = await tab.find(text, best_match=True, timeout=timeout)
                elements = [match] if match else []
            else:
                elements = await tab.find_all(text, timeout=timeout)
        elif selector:
            if frame_element is None:
                elements = await tab.select_all(selector, timeout=timeout)
            else:
                deadline = asyncio.get_running_loop().time() + timeout
                while True:
                    elements = await frame_element.query_selector_all(selector)
                    if elements or asyncio.get_running_loop().time() >= deadline:
                        break
                    await asyncio.sleep(0.05)
        elif frame_element is not None:
            elements = await _frame_xpath(tab, frame_element, xpath, timeout, limit)
        else:
            try:
                elements = await tab.xpath(xpath, timeout=timeout)
            except uc.ProtocolException as exc:
                if exc.code != -32601:
                    raise
                # Native xpath still passes the removed _is_update positional
                # flag as _attach in 0.50.6. Reuse its native search primitive.
                elements = await tab.find_all(xpath, timeout=timeout)
        if frame_element is not None and not xpath:
            frame_nodes = uc.util.filter_recurse_all(
                frame_element.content_document, lambda node: True
            )
            allowed = {node.backend_node_id for node in frame_nodes}
            elements = [
                element
                for element in elements or []
                if element.backend_node_id in allowed
            ]
            if text and best_match and elements:
                elements = [
                    min(
                        elements,
                        key=lambda element: abs(len(text) - len(element.text_all)),
                    )
                ]
        s._snapshot_id += 1
        snapshot_id = s._snapshot_id
        page_id = str(page.target_id)
        result = []
        for index, element in enumerate((elements or [])[:limit]):
            if element is None:
                continue
            uid = f"{snapshot_id}_find_{index}"
            s._uid_to_backend_node_id[uid] = int(element.backend_node_id)
            s._uid_owners[uid] = (
                page_id,
                element.tab,
                frame_id or "",
            )
            result.append(
                {
                    "uid": uid,
                    "tag": element.tag_name,
                    "text": element.text_all,
                    "attributes": dict(element.attrs),
                }
            )
        return result

    @mcp.tool()
    async def create_browser_context(
        name: str, proxy_server: str = "", proxy_bypass_list: list[str] | None = None
    ) -> str:
        """Use native Browser.create_context, including authenticated HTTP/SOCKS proxies."""
        if not name:
            raise ValueError("Supply a new, nonempty context name.")
        async with s._browser_lock:
            browser = await s._ensure_browser()
            if name in s._named_browser_contexts:
                raise ValueError("Supply a new, nonempty context name.")
            tab = await browser.create_context(
                url="about:blank",
                new_tab=False,
                new_window=True,
                proxy_server=proxy_server or None,
                proxy_bypass_list=",".join(proxy_bypass_list)
                if proxy_bypass_list
                else None,
            )
            s._named_browser_contexts[name] = tab.target.browser_context_id
            s._selected_target_id = str(tab.target_id)
            await s._prepare_page(tab)
            await s._auto_enable_network_collection(tab)
        return f"Created and selected context '{name}'." + await s._format_pages()

    @mcp.tool()
    async def close_browser_context(name: str) -> str:
        """Dispose a named isolated context and its pages through official CDP."""
        async with s._browser_lock:
            browser = await s._ensure_browser()
            context = s._named_browser_contexts.get(name)
            if context is None:
                raise ValueError("Unknown context name.")
            await browser.update_targets()
            pages = [
                tab for tab in browser.tabs if tab.target.browser_context_id == context
            ]
            await browser.send(uc.cdp.target.dispose_browser_context(context))
            for page in pages:
                await s._forget_page_state(page)
                await page.aclose()
            del s._named_browser_contexts[name]
            await browser.update_targets()
        return f"Closed context '{name}'." + await s._format_pages()

    @s._page_tool()
    async def set_cookies(
        cookies: list[dict], *, page_id: int | str | None = None
    ) -> str:
        """Batch set cookies using native CookieJar.set_all and official CookieParam types.

        Entries accept CDP camelCase or Python snake_case fields, including
        expires, httpOnly, sameSite and partitionKey.
        """
        typed = [s._cookie_param(cookie) for cookie in cookies]
        await (await s._cookie_jar()).set_all(typed)
        return f"Set {len(typed)} cookies in the selected context."

    @s._page_tool()
    async def clear_cookies(*, page_id: int | str | None = None) -> str:
        """Clear the selected context's cookies through native CookieJar.clear."""
        await (await s._cookie_jar()).clear()
        return "Cookies cleared in the selected context."

    @s._page_tool()
    async def save_cookies(file_path: str, *, page_id: int | str | None = None) -> str:
        """Save context cookies with native CookieJar.save (nodriver pickle format)."""
        await (await s._cookie_jar()).save(file_path)
        return f"Native cookies saved to {file_path}."

    @s._page_tool()
    async def load_cookies(
        file_path: str, pattern: str = ".*", *, page_id: int | str | None = None
    ) -> str:
        """Load a locally created native cookie file through CookieJar.load."""
        await (await s._cookie_jar()).load(file_path, pattern=pattern)
        return f"Native cookies loaded from {file_path}."

    @s._page_tool()
    async def configure_network_capture(
        durable: bool = True,
        max_total_buffer_size: int = 20000000,
        max_resource_buffer_size: int = 5000000,
        *,
        page_id: int | str | None = None,
    ) -> str:
        """Configure native durable response bodies to survive cross-process navigation."""
        if max_total_buffer_size <= 0 or max_resource_buffer_size <= 0:
            raise ValueError("Buffer sizes must be positive.")
        tab = await s._active_tab()
        await s._auto_enable_network_collection(tab)
        await tab.send(
            uc.cdp.network.configure_durable_messages(
                max_total_buffer_size=max_total_buffer_size if durable else None,
                max_resource_buffer_size=max_resource_buffer_size if durable else None,
            )
        )
        return f"Durable network capture {'enabled' if durable else 'disabled'}."

    @mcp.tool()
    async def list_cdp_commands(
        domain: str = "", query: str = "", offset: int = 0, limit: int = 50
    ) -> dict:
        """Discover every official CDP command and its typed parameters.

        Domains include WebMCP, SmartCardEmulation, DigitalCredentials, Ads,
        Emulation, DOM, CSS, Network, Storage and all other installed bindings.
        This describes bindings; experimental availability depends on Chrome.
        """
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("Invalid offset or limit.")
        commands = describe_commands(domain, query)
        return {
            "total": len(commands),
            "offset": offset,
            "commands": commands[offset : offset + limit],
        }

    @s._page_tool(
        prepare=_prepare_cdp_command,
        description=(
            "Invoke an official generated CDP command with JSON parameters and typed conversion. "
            "Use list_cdp_commands for names/parameters. Top-level fields accept snake_case or "
            "CDP camelCase; nested objects use CDP JSON names. Use browser scope for "
            "browser-wide commands. Experimental errors come from Chrome. "
            "Runtime.enable requires allow_runtime_enable=true (default false). "
            "Runtime.evaluate and ordinary CDP commands do not require this opt-in. "
            + RUNTIME_ENABLE_WARNING
        ),
    )
    async def send_cdp_command(
        domain: str,
        command: str,
        parameters: dict | None = None,
        scope: str = "page",
        timeout: int = 30000,
        *,
        allow_runtime_enable: StrictBool = False,
        page_id: int | str | None = None,
    ) -> Any:
        """Invoke an official generated command with JSON parameters and typed conversion.

        Use list_cdp_commands for names and parameters. Top-level fields accept
        snake_case or CDP camelCase; nested objects use CDP JSON names. Use browser
        scope for browser-wide commands. Experimental errors come from Chrome.
        """
        generator = build_command(domain, command, parameters)
        target = await connection(scope)
        runtime_switch = domain_module(
            domain
        ) is uc.cdp.runtime and generator.gi_code.co_name in {"enable", "disable"}
        lock = s._collection_lock(target) if runtime_switch else None
        async with lock if lock is not None else nullcontext():
            if runtime_switch:
                s._check_collection_owner(target, lock)
                s._frame_contexts.watch(target)
                enabled = generator.gi_code.co_name == "enable"
            try:
                result = await s._await_with_timeout(
                    target.send(generator), timeout, f"{domain}.{command}"
                )
            except BaseException:
                if runtime_switch:
                    # The command may have reached Chrome before its reply was
                    # lost. Restore the prior consumers' state before unlocking.
                    await s._frame_contexts.restore_runtime(target)
                raise
            if runtime_switch:
                s._check_collection_owner(target, lock)
                s._frame_contexts.request_runtime(target, enabled)
            return json_value(result)

    @mcp.tool()
    async def list_cdp_events(domain: str = "") -> list[dict]:
        """Discover generated CDP events for subscriptions, including new domains."""
        result = []
        for name, cls in sorted(event_types(domain).items()):
            description = inspect.getdoc(cls) or ""
            if name == "ServiceWorker.workerVersionUpdated":
                description = (
                    description + "\n\n" + _SERVICE_WORKER_ROUTER_NOTE
                ).strip()
            result.append({"event": name, "description": description})
        return result

    @s._page_tool(
        prepare=_prepare_cdp_subscription,
        description=(
            "Subscribe to official CDP events. Network/WebSocket events do not enable Runtime. "
            "Domains with a parameterless enable command are enabled automatically; "
            "otherwise enable/configure the domain with send_cdp_command first. "
            "Auto-enabling Runtime for Runtime.* events requires allow_runtime_enable=true "
            "(default false). enable_domain=false only registers callbacks; it cannot "
            "receive Runtime events unless Runtime is already enabled. "
            + RUNTIME_ENABLE_WARNING
            + "\n\n"
            + _SERVICE_WORKER_ROUTER_NOTE
        ),
    )
    async def subscribe_cdp_events(
        events: list[str],
        scope: str = "page",
        enable_domain: bool = True,
        buffer_size: int = 200,
        *,
        allow_runtime_enable: StrictBool = False,
        page_id: int | str | None = None,
    ) -> str:
        """Subscribe to official CDP events. Runtime is enabled only when requested here.

        For domains with a parameterless enable command it runs automatically.
        Otherwise enable/configure the domain with send_cdp_command first.
        """
        if not events or not 1 <= buffer_size <= 10000:
            raise ValueError("Supply events and a buffer_size between 1 and 10000.")
        parsed = [resolve_event(name) for name in events]
        target = await connection(scope)
        lock = s._collection_lock(target)
        async with lock:
            s._check_collection_owner(target, lock)
            return await start_subscription(
                parsed, target, scope, enable_domain, buffer_size, lock
            )

    async def start_subscription(
        parsed, target, scope, enable_domain, buffer_size, lock
    ):
        entry = {
            "connection": target,
            "page_id": s._page_key(target) if scope == "page" else None,
            "callbacks": [],
            "events": deque(maxlen=buffer_size),
            "sequence": itertools.count(1),
        }
        domains = set()
        for name, cls in parsed:

            def callback(event, method=name):
                entry["events"].append(
                    {
                        "id": next(entry["sequence"]),
                        "method": method,
                        "data": json_value(event),
                    }
                )

            # nodriver optionally supplies the connection as the second argument.
            def handler(event, _connection=None, callback=callback):
                callback(event)

            # A listener-only subscription must not make failure recovery or
            # console shutdown activate/retain Runtime on its behalf.
            handler._runtime_passive = not enable_domain
            target.add_handler(cls, handler)
            entry["callbacks"].append((cls, handler))
            domains.add(cls.__module__.rsplit(".", 1)[-1])
        try:
            if enable_domain:
                for domain in sorted(domains):
                    module = domain_module(domain)
                    enable = getattr(module, "enable", None)
                    if enable is not None:
                        try:
                            inspect.signature(enable).bind()
                        except TypeError as exc:
                            raise ValueError(
                                f"Enable {domain} with send_cdp_command first."
                            ) from exc
                        await target.send(enable())
            s._check_collection_owner(target, lock)
        except BaseException:
            for cls, callback in entry["callbacks"]:
                if callback in target.handlers.get(cls, []):
                    target.remove_handler(cls, callback)
            raise
        identifier = str(next(subscription_ids))
        subscriptions[identifier] = entry
        return identifier

    @s._page_tool(
        description=(
            "Read buffered events with stable per-subscription IDs.\n\n"
            + _SERVICE_WORKER_ROUTER_NOTE
        )
    )
    async def get_cdp_events(
        subscription_id: str,
        after_id: int = 0,
        clear: bool = False,
        *,
        page_id: int | str | None = None,
    ) -> list[dict]:
        """Read buffered events with stable per-subscription IDs."""
        entry = subscriptions.get(subscription_id)
        if entry is None:
            raise ValueError("Unknown subscription_id.")
        if page_id is not None and entry["page_id"] != s._page_key(await s._page_tab()):
            raise ValueError(
                "Subscription belongs to a different page_id or browser scope."
            )
        result = [event for event in entry["events"] if event["id"] > after_id]
        if clear:
            entry["events"].clear()
        return result

    @s._page_tool()
    async def unsubscribe_cdp_events(
        subscription_id: str, *, page_id: int | str | None = None
    ) -> str:
        """Remove exactly this subscription's callbacks without disabling other consumers."""
        entry = subscriptions.get(subscription_id)
        if entry is None:
            raise ValueError("Unknown subscription_id.")
        if page_id is not None and entry["page_id"] != s._page_key(await s._page_tab()):
            raise ValueError(
                "Subscription belongs to a different page_id or browser scope."
            )
        target = entry["connection"]
        lock = s._collection_lock(target)
        async with lock:
            s._check_collection_owner(target, lock)
            if subscriptions.pop(subscription_id, None) is not entry:
                raise ValueError("Unknown subscription_id.")
            for cls, callback in entry["callbacks"]:
                target.remove_handler(cls, callback)
        return "Subscription removed."

    async def web_state():
        tab = await s._active_tab()
        key = s._tab_key(tab)
        if key in web_states:
            return web_states[key]
        state = {
            "connection": tab,
            "tools": {},
            "results": {},
            "pending": {},
            "callbacks": [],
        }

        def added(event):
            for tool in event.tools:
                state["tools"][(str(tool.frame_id), tool.name)] = tool

        def removed(event):
            for tool in event.tools:
                state["tools"].pop((str(tool.frame_id), tool.name), None)

        def responded(event):
            state["results"][event.invocation_id] = json_value(event)
            while len(state["results"]) > 200:
                state["results"].pop(next(iter(state["results"])))
            future = state["pending"].get(event.invocation_id)
            if future is not None and not future.done():
                future.set_result(json_value(event))

        state["callbacks"] = [
            (uc.cdp.web_mcp.ToolsAdded, added),
            (uc.cdp.web_mcp.ToolsRemoved, removed),
            (uc.cdp.web_mcp.ToolResponded, responded),
        ]
        for event, callback in state["callbacks"]:
            tab.add_handler(event, callback)
        try:
            await tab.send(uc.cdp.web_mcp.enable())
        except BaseException:
            for event, callback in state["callbacks"]:
                tab.remove_handler(event, callback)
            raise
        web_states[key] = state
        return state

    @s._page_tool()
    async def list_webmcp_tools(*, page_id: int | str | None = None) -> list[dict]:
        """Discover business tools explicitly registered by the current website."""
        state = await web_state()
        return [tool.to_json() for tool in state["tools"].values()]

    @s._page_tool()
    async def call_webmcp_tool(
        tool_name: str,
        input: dict | None = None,
        frame_id: str = "",
        wait_for_result: bool = True,
        timeout: int = 30000,
        *,
        page_id: int | str | None = None,
    ) -> dict:
        """Invoke a website's WebMCP tool and return its result or invocation ID."""
        state = await web_state()
        matches = [
            tool
            for tool in state["tools"].values()
            if tool.name == tool_name
            and (not frame_id or str(tool.frame_id) == frame_id)
        ]
        if len(matches) != 1:
            raise ValueError(
                "Tool not found or ambiguous; use list_webmcp_tools and supply frame_id."
            )
        tab = state["connection"]
        identifier = await tab.send(
            uc.cdp.web_mcp.invoke_tool(matches[0].frame_id, tool_name, input or {})
        )
        if not wait_for_result:
            return {"invocation_id": identifier}
        if identifier in state["results"]:
            return state["results"][identifier]
        future = asyncio.get_running_loop().create_future()
        state["pending"][identifier] = future
        try:
            return await s._await_with_timeout(future, timeout, "WebMCP tool")
        finally:
            state["pending"].pop(identifier, None)

    @s._page_tool()
    async def get_webmcp_invocation(
        invocation_id: str, *, page_id: int | str | None = None
    ) -> dict:
        """Read the result of an asynchronous website tool invocation."""
        state = await web_state()
        return state["results"].get(
            invocation_id, {"invocation_id": invocation_id, "status": "Pending"}
        )

    @s._page_tool()
    async def cancel_webmcp_invocation(
        invocation_id: str, *, page_id: int | str | None = None
    ) -> str:
        """Cancel a pending website tool through the official WebMCP command."""
        state = await web_state()
        await state["connection"].send(uc.cdp.web_mcp.cancel_invocation(invocation_id))
        return f"Cancellation requested for {invocation_id}."

    @s._page_tool()
    async def start_screen_recording(
        file_path: str,
        audio: bool = False,
        max_width: int = 0,
        max_height: int = 0,
        frame_rate: int = 0,
        *,
        page_id: int | str | None = None,
    ) -> str:
        """Start native Page screen recording; stop_screen_recording writes its stream."""
        tab = await s._page_tab()
        key = str(tab.target_id)
        if key in recordings:
            raise ValueError("A recording is already active on this page.")
        recording = {"connection": tab, "file_path": file_path, "starting": True}
        recordings[key] = recording
        try:
            recording["handle"] = await tab.send(
                uc.cdp.page.start_screen_recording(
                    audio=audio,
                    max_width=max_width or None,
                    max_height=max_height or None,
                    frame_rate=frame_rate or None,
                )
            )
            recording["starting"] = False
        except BaseException:
            recordings.pop(key, None)
            raise
        return "Screen recording started."

    @s._page_tool()
    async def stop_screen_recording(
        file_path: str = "", *, page_id: int | str | None = None
    ) -> str:
        """Stop native screen recording and save bytes without requiring ffmpeg."""
        if not recordings:
            raise ValueError("No recording is active.")
        if page_id is None and len(recordings) == 1:
            key = next(iter(recordings))
        else:
            key = str((await s._page_tab()).target_id)
        recording = recordings.get(key)
        if recording is None:
            raise ValueError("No recording is active on this page.")
        if recording["starting"] or recording.get("stopping"):
            raise ValueError("This recording is still starting or already stopping.")
        recording["stopping"] = True
        tab = recording["connection"]
        path = file_path or recording["file_path"]
        try:
            handle = await tab.send(uc.cdp.page.stop_screen_recording())
            data = await s._read_stream(tab, handle)
            Path(path).write_bytes(data)
            if recording["handle"] != handle:
                await tab.send(uc.cdp.io.close(recording["handle"]))
            return f"Recording saved to {path} ({len(data)} bytes)."
        finally:
            recordings.pop(key, None)

    @s._page_tool()
    async def get_annotated_page_content(
        file_path: str = "",
        include_actionable_information: bool = True,
        *,
        page_id: int | str | None = None,
    ) -> dict:
        """Get Chrome's annotated content as base64 protobuf, or save its binary form.

        This experimental format is distinct from the readable take_snapshot output.
        """
        import base64

        data = await (await s._active_tab()).send(
            uc.cdp.page.get_annotated_page_content(
                include_actionable_information=include_actionable_information
            )
        )
        if file_path:
            Path(file_path).write_bytes(base64.b64decode(data))
            return {"file_path": file_path, "format": "AnnotatedPageContent protobuf"}
        return {
            "format": "AnnotatedPageContent protobuf",
            "encoding": "base64",
            "data": data,
        }

    @mcp.tool()
    async def list_extensions() -> list[dict]:
        """List extensions using the new official Extensions.getExtensions command."""
        return json_value(
            await (await s._get_browser()).send(uc.cdp.extensions.get_extensions())
        )

    @s._page_tool()
    async def trigger_extension_action(
        extension_id: str, *, page_id: int | str | None = None
    ) -> str:
        """Bring the requested page to front and trigger its extension action via CDP."""
        page = await s._page_tab()
        browser = await s._get_browser()
        target_id = await _extension_tab_target(browser, page)
        async with extension_actions:
            # Chrome invokes the toolbar action on the window's active tab,
            # even when triggerAction names another tab in that window.
            await page.activate()
            await browser.send(
                uc.cdp.extensions.trigger_action(
                    id_=extension_id, target_id=str(target_id)
                )
            )
        return "Extension action triggered."

    @s._page_tool()
    async def get_ad_info(
        include_scripts: bool = False, *, page_id: int | str | None = None
    ) -> dict:
        """Read native ad metrics and optionally the page's ad script details."""
        tab = await s._active_tab()
        result = {"metrics": json_value(await tab.send(uc.cdp.ads.get_ad_metrics()))}
        if include_scripts:
            result["scripts"] = json_value(await tab.send(uc.cdp.ads.get_ad_scripts()))
        return result

    @s._page_tool()
    async def get_crash_report_context(*, page_id: int | str | None = None) -> Any:
        """Read context entries with the new CrashReportContext domain."""
        return json_value(
            await (await s._active_tab()).send(
                uc.cdp.crash_report_context.get_entries()
            )
        )
