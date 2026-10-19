"""Bounded navigation and DOM observation around trusted browser actions."""

from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from nodriver import cdp


class ActionInterrupted(ValueError):
    """An action is waiting for a dialog that the caller must handle."""


@dataclass
class ActionResult:
    url: str = ""
    opened_pages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = []
        if self.url:
            lines.append(f"Navigation observed: {self.url}")
        if self.opened_pages:
            lines.append("Opened page_ids: " + ", ".join(self.opened_pages))
        return "\n".join(lines + self.warnings)


class ActionWaiter:
    """Observe before an action, then wait for navigation and a quiet DOM.

    The observation windows follow the upstream WaitForHelper design: a short
    navigation probe, a bounded navigation wait, then bounded DOM stability.
    Network idleness is deliberately not required for polling/streaming pages.
    """

    def __init__(
        self,
        tab,
        page,
        browser,
        evaluate: Callable[[Any, str], Awaitable[Any]],
        on_navigation: Callable[[str], None],
        *,
        frame_id: str | None = None,
        wait_for_stable: bool = True,
        probe: float = 0.1,
        navigation_timeout: float = 3,
        dom_timeout: float = 3,
        quiet: float = 0.1,
        ignored_dialogs: frozenset[str] = frozenset(),
    ):
        self.tab, self.page, self.browser = tab, page, browser
        self.evaluate, self.on_navigation = evaluate, on_navigation
        self.frame_id, self.wait_for_stable = frame_id, wait_for_stable
        self.probe, self.navigation_timeout = probe, navigation_timeout
        self.dom_timeout, self.quiet = dom_timeout, quiet
        self.ignored_dialogs = ignored_dialogs
        self.result = ActionResult()
        self.navigating, self.loaded = asyncio.Event(), asyncio.Event()
        self.dialog = asyncio.get_running_loop().create_future()
        self.callbacks = []
        self.frame_ids = {str(page.target_id), str(tab.target_id)}
        if frame_id:
            self.frame_ids.add(frame_id)
        self.notified = set()
        self.root_frame_id = str(page.target_id)
        self.popup_requested = False
        self.initial_targets = {str(t.target_id) for t in getattr(browser, "tabs", [])}
        self.observer_key = "__mcp_dom_" + secrets.token_hex(8)
        self.observing = False

    def listen(self, connection, event, callback):
        connection.add_handler(event, callback)
        self.callbacks.append((connection, event, callback))

    def started(self, event):
        frame_id = str(event.frame_id)
        kind = getattr(event, "navigation_type", None)
        if getattr(kind, "value", kind) in {"sameDocument", "historySameDocument"}:
            return
        if frame_id in self.frame_ids:
            if not self.navigating.is_set():
                self.loaded.clear()
            self.navigating.set()
            if (
                isinstance(event, cdp.page.FrameStartedNavigating)
                and frame_id not in self.notified
            ):
                self.notified.add(frame_id)
                self.on_navigation(frame_id)

    def navigated(self, event):
        if str(event.frame.id_) in self.frame_ids:
            self.navigating.set()
            self.result.url = event.frame.url

    def stopped(self, event):
        if str(event.frame_id) in self.frame_ids:
            self.loaded.set()

    def same_document(self, event):
        if str(event.frame_id) in self.frame_ids:
            self.navigating.set()
            self.loaded.set()
            self.result.url = event.url

    def dialog_opened(self, event):
        if getattr(event.type_, "value", event.type_) in self.ignored_dialogs:
            return
        if not self.dialog.done():
            self.dialog.set_result(event)

    def popup(self, event):
        info = event.target_info
        if str(info.opener_id) == str(self.page.target_id) and info.type_ == "page":
            self.result.opened_pages.append(str(info.target_id))

    def window_opened(self, _event):
        self.popup_requested = True

    async def discover_popups(self):
        if not self.popup_requested:
            return
        try:
            async with asyncio.timeout(0.75):
                while True:
                    targets = await self.browser.send(cdp.target.get_targets())
                    for info in targets:
                        target_id = str(info.target_id)
                        if (
                            info.type_ == "page"
                            and str(info.opener_id) == str(self.page.target_id)
                            and target_id not in self.initial_targets
                            and target_id not in self.result.opened_pages
                        ):
                            self.result.opened_pages.append(target_id)
                    if self.result.opened_pages:
                        return
                    await asyncio.sleep(0.02)
        except TimeoutError:
            self.result.warnings.append(
                "A popup was requested; use list_pages to discover it."
            )

    async def start(self):
        for connection in {id(t): t for t in (self.page, self.tab)}.values():
            for event, callback in (
                (cdp.page.FrameStartedNavigating, self.started),
                (cdp.page.FrameRequestedNavigation, self.started),
                (cdp.page.FrameNavigated, self.navigated),
                (cdp.page.FrameStoppedLoading, self.stopped),
                (cdp.page.NavigatedWithinDocument, self.same_document),
                (cdp.page.JavascriptDialogOpening, self.dialog_opened),
                (cdp.page.WindowOpen, self.window_opened),
            ):
                self.listen(connection, event, callback)
            await asyncio.wait_for(connection.send(cdp.page.enable()), 2)
            tree = await asyncio.wait_for(connection.get_frame_tree(), 2)
            self.frame_ids.add(str(tree.frame.id_))
            if connection is self.page:
                self.root_frame_id = str(tree.frame.id_)
        self.listen(self.browser, cdp.target.TargetCreated, self.popup)

    async def guarded(self, awaitable, timeout: float | None = None):
        task = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait(
                (task, self.dialog),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                return task.result()
            if self.dialog in done:
                event = self.dialog.result()
                kind = getattr(event.type_, "value", event.type_)
                raise ActionInterrupted(
                    f"Action interrupted by {kind} dialog: {event.message}. "
                    "Use handle_dialog on the same page_id."
                )
            raise TimeoutError("Action observation timed out.")
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def stable_dom(self):
        key = json.dumps(self.observer_key)
        expression = """(() => new Promise(resolve => {
            const key = %s;
            let quietTimer, limitTimer, done = false;
            const finish = state => {
                if (done) return;
                done = true;
                observer.disconnect();
                clearTimeout(quietTimer); clearTimeout(limitTimer);
                delete globalThis[key]; resolve(state);
            };
            const changed = () => {
                clearTimeout(quietTimer);
                quietTimer = setTimeout(() => finish('stable'), %d);
            };
            const observer = new MutationObserver(changed);
            globalThis[key] = () => finish('cancelled');
            observer.observe(document.documentElement || document, {
                childList:true, subtree:true, attributes:true, characterData:true
            });
            limitTimer = setTimeout(() => finish('limit'), %d);
            changed();
        }))()""" % (key, int(self.quiet * 1000), int(self.dom_timeout * 1000))
        self.observing = True
        state = await self.guarded(
            self.evaluate(self.tab, expression), self.dom_timeout + 0.25
        )
        self.observing = False
        if state == "limit":
            self.result.warnings.append("DOM stability wait reached its time limit.")

    async def run(self, action):
        try:
            await self.start()
            value = await self.guarded(action())
            try:
                await self.guarded(self.navigating.wait(), self.probe)
            except TimeoutError:
                pass
            if self.navigating.is_set():
                try:
                    await self.guarded(self.loaded.wait(), self.navigation_timeout)
                except TimeoutError:
                    self.result.warnings.append(
                        "Navigation wait reached its time limit."
                    )
            if self.wait_for_stable:
                try:
                    await self.stable_dom()
                except Exception as exc:
                    if isinstance(exc, ActionInterrupted):
                        raise
                    self.result.warnings.append(
                        f"DOM stability could not be confirmed: {exc}"
                    )
            await self.discover_popups()
            return value
        finally:
            for connection, event, callback in reversed(self.callbacks):
                connection.remove_handler(event, callback)
            self.callbacks.clear()
            if self.observing:
                try:
                    await asyncio.wait_for(
                        self.evaluate(
                            self.tab, f"globalThis[{json.dumps(self.observer_key)}]?.()"
                        ),
                        0.25,
                    )
                except Exception:
                    pass
            if not self.dialog.done():
                self.dialog.cancel()
